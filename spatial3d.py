#!/usr/bin/env python3
import serial, struct, threading, time, math, json, asyncio
import websockets
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys

PORT_SERIAL = "COM11"
BAUD        = 115200
WS_PORT     = 8767
HTTP_PORT   = 8080

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>CTW 3D Radar</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#000; overflow:hidden; font-family:monospace; }
#info {
  position:absolute; top:10px; left:10px; color:#0ff;
  font-size:12px; pointer-events:none; text-shadow:0 0 8px #0ff;
  z-index:10;
}
</style>
</head>
<body>
<div id="info">
  <div>CTW SENTINEL — 3D SPATIAL MONITOR</div>
  <div>Left-drag: rotate | Right-drag: tilt | Scroll: zoom</div>
  <div id="status">Connecting...</div>
  <div id="targets"></div>
</div>
<canvas id="canvas" style="display:block"></canvas>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
const W=window.innerWidth, H=window.innerHeight;
const renderer=new THREE.WebGLRenderer({canvas:document.getElementById('canvas'),antialias:true});
renderer.setSize(W,H);
renderer.setPixelRatio(window.devicePixelRatio);
const scene=new THREE.Scene();
scene.background=new THREE.Color(0x000510);
scene.fog=new THREE.Fog(0x000510,8,20);
const camera=new THREE.PerspectiveCamera(60,W/H,0.001,100);
camera.position.set(0,-3,3);
camera.lookAt(0,0,0);

scene.add(new THREE.GridHelper(6,24,0x003333,0x001a1a));

const axLine=(p1,p2,color)=>{
  const g=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...p1),new THREE.Vector3(...p2)]);
  return new THREE.Line(g,new THREE.LineBasicMaterial({color}));
};
// Clickable axis lines — click to reorient Z to that axis
const axMeshes = [];
function makeAxis(dir, color, label){
  const pts = [new THREE.Vector3(0,0,0), new THREE.Vector3(...dir)];
  const geo  = new THREE.TubeGeometry(
    new THREE.CatmullRomCurve3(pts), 8, 0.012, 6, false);
  const mat  = new THREE.MeshBasicMaterial({color});
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData = {dir, label};
  scene.add(mesh);
  axMeshes.push(mesh);
  return mesh;
}
makeAxis([3,0,0], 0xff3333, 'X');
makeAxis([0,3,0], 0x33ff33, 'Y');
makeAxis([0,0,3], 0x3333ff, 'Z');

// Axis reorientation on click
const raycaster = new THREE.Raycaster();
raycaster.params.Line = {threshold:0.05};
const mouse = new THREE.Vector2();

// Current axis remapping: sensor axes → three.js axes
let axisRemap = {x:'x', y:'y', z:'z'};

window.addEventListener('click', e=>{
  // ignore if we were dragging
  if(Math.abs(e.clientX-lastX)>3 || Math.abs(e.clientY-lastY)>3) return;
  mouse.x = (e.clientX/window.innerWidth)*2-1;
  mouse.y = -(e.clientY/window.innerHeight)*2+1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(axMeshes);
  if(!hits.length) return;
  const axis = hits[0].object.userData.label;
  // Rotate so clicked axis becomes the forward/depth (Z) axis
  if(axis==='X'){
    upAxis.set(1,0,0);
    axisRemap = {x:'z', y:'x', z:'y'};
    camQuat.setFromAxisAngle(new THREE.Vector3(0,0,1), Math.PI/2);
  } else if(axis==='Y'){
    upAxis.set(0,1,0);
    axisRemap = {x:'x', y:'y', z:'z'};
    camQuat.set(0,0,0,1);
  } else {
    upAxis.set(0,0,1);
    axisRemap = {x:'x', y:'z', z:'y'};
    camQuat.setFromAxisAngle(new THREE.Vector3(1,0,0), -Math.PI/2);
  }
  updateCamera();
  // Clear dots since they're in old orientation
  allDots.forEach(d=>scene.remove(d));
  allDots.length=0;
  document.getElementById('status').textContent=
    `REORIENTED: ${axis} axis is now depth`;
  setTimeout(()=>document.getElementById('status').textContent='CONNECTED',2000);
});

const sensor=new THREE.Mesh(
  new THREE.SphereGeometry(0.06,16,16),
  new THREE.MeshPhongMaterial({
    color:0x0044aa,
    emissive:0x001133,
    specular:0x4488ff,
    shininess:80
  }));
sensor.position.set(0,0,0);
scene.add(sensor);

for(let r=1;r<=3;r++){
  const pts=[];
  for(let a=0;a<=360;a+=5){
    const rad=a*Math.PI/180;
    pts.push(new THREE.Vector3(Math.cos(rad)*r,Math.sin(rad)*r,0));
  }
  scene.add(new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({color:0x002244,transparent:true,opacity:0.6})));
}

const COLORS=[0x00ff44,0x00cc33,0x00ff88,0x44ff44,0x88ff00];
const tmeshes={}, ttrails={};
const TRAIL=40;

const allDots    = [];
let   tare_until = 0;

function remapAxes(sx,sy,sz){
  // axisRemap.x = which sensor axis goes to Three.js X slot
  const src={x:sx, y:sy, z:sz};
  return [src[axisRemap.x], src[axisRemap.y], src[axisRemap.z]];
}

function addDot(x,y,z){
  if(Date.now()<tare_until) return;
  const [rx,ry,rz]=remapAxes(x,y,z);
  const geo=new THREE.SphereGeometry(0.003,4,4);
  const mat=new THREE.MeshBasicMaterial({
    color:0x00ff44,
    transparent:true,
    opacity:0.75
  });
  const mesh=new THREE.Mesh(geo,mat);
  mesh.position.set(rx,ry,rz);
  scene.add(mesh);
  allDots.push(mesh);
}

function updateTarget(tid,x,y,z){
  addDot(x,y,z);
}

let ws;
function connect(){
  ws=new WebSocket('ws://localhost:8767');
  ws.onopen=()=>document.getElementById('status').textContent='CONNECTED';
  ws.onclose=()=>{document.getElementById('status').textContent='RECONNECTING...';setTimeout(connect,1000);};
  ws.onmessage=(e)=>{
    const d=JSON.parse(e.data);
    if(d.type==='targets'){
      d.targets.forEach(t=>updateTarget(t.tid,t.x,t.y,t.z));
      document.getElementById('targets').innerHTML=
        d.targets.map(t=>`TID=${t.tid} X=${t.x.toFixed(3)} Y=${t.y.toFixed(3)} Z=${t.z.toFixed(3)} ${t.rng.toFixed(3)}m`).join('<br>')||'(no targets)';
    }
  };
}
connect();

let isDragging = false, lastX = 0, lastY = 0, radius = 5;
let flatMode   = false;

// Pure trackball — no poles, always rotates around 0,0,0
// in screen-aligned axes so drag direction = scene movement direction
const camQuat = new THREE.Quaternion();

function updateCamera(){
  const pos = new THREE.Vector3(0, 0, radius).applyQuaternion(camQuat);
  camera.position.copy(pos);
  camera.up.copy(new THREE.Vector3(0,1,0).applyQuaternion(camQuat));
  camera.lookAt(0,0,0);
}
updateCamera();

document.addEventListener('mousedown', e=>{
  const id = e.target.id;
  if(id==='flatcheck'||id==='flatlabel'||
     id==='tarebtn'||id==='clearbtn'||id==='taredur') return;
  isDragging=true; lastX=e.clientX; lastY=e.clientY;
});
document.addEventListener('mouseup', ()=>isDragging=false);
document.addEventListener('mousemove', e=>{
  if(!isDragging||flatMode) return;
  const dx = e.clientX-lastX;
  const dy = e.clientY-lastY;
  lastX=e.clientX; lastY=e.clientY;
  if(dx===0&&dy===0) return;

  // Rotate around screen-right axis for dy, screen-up axis for dx
  // These are computed from current camera orientation so they
  // always match the drag direction regardless of current view
  const right = new THREE.Vector3(1,0,0).applyQuaternion(camQuat);
  const up    = new THREE.Vector3(0,1,0).applyQuaternion(camQuat);

  const dqX = new THREE.Quaternion().setFromAxisAngle(up,    -dx*0.006);
  const dqY = new THREE.Quaternion().setFromAxisAngle(right, -dy*0.006);

  camQuat.premultiply(dqX).premultiply(dqY);
  camQuat.normalize();
  updateCamera();
});
document.addEventListener('wheel', e=>{
  radius = Math.max(0.001, radius + e.deltaY*0.005);
  updateCamera();
},{passive:true});
document.addEventListener('contextmenu', e=>e.preventDefault());

// Tare control
const tareDiv=document.createElement('div');
tareDiv.style.cssText='position:absolute;bottom:35px;left:10px;color:#0f0;font-family:monospace;font-size:12px;z-index:20;display:flex;gap:6px;align-items:center;';
tareDiv.innerHTML=`
  <button id="tarebtn" style="background:#001a00;color:#0f0;border:1px solid #0f0;font-family:monospace;padding:2px 8px;cursor:pointer;">TARE</button>
  <input id="taredur" type="number" value="5" min="1" max="60" style="width:36px;background:#000;color:#0f0;border:1px solid #0f0;font-family:monospace;text-align:center;">
  <span>sec</span>
  <button id="clearbtn" style="background:#001a00;color:#0f0;border:1px solid #0f0;font-family:monospace;padding:2px 8px;cursor:pointer;">CLEAR ALL</button>
`;
document.body.appendChild(tareDiv);
document.getElementById('tarebtn').addEventListener('click',()=>{
  const dur=parseInt(document.getElementById('taredur').value)||5;
  tare_until=Date.now()+dur*1000;
  allDots.forEach(d=>scene.remove(d));
  allDots.length=0;
  document.getElementById('status').textContent=`TARING ${dur}s...`;
  setTimeout(()=>document.getElementById('status').textContent='CONNECTED',dur*1000);
});
document.getElementById('clearbtn').addEventListener('click',()=>{
  allDots.forEach(d=>scene.remove(d));
  allDots.length=0;
});

// Flat mode checkbox
const flatDiv=document.createElement('div');
flatDiv.style.cssText='position:absolute;bottom:10px;left:10px;color:#0f0;font-family:monospace;font-size:12px;z-index:20;';
flatDiv.innerHTML='<input type="checkbox" id="flatcheck"> <label id="flatlabel" for="flatcheck">LOCK FLAT</label>';
document.body.appendChild(flatDiv);
document.getElementById('flatcheck').addEventListener('change',e=>{
  flatMode=e.target.checked;
  if(flatMode){
    camQuat.set(0,0,0,1);
    updateCamera();
  }
});
window.addEventListener('resize',()=>{
  renderer.setSize(window.innerWidth,window.innerHeight);
  camera.aspect=window.innerWidth/window.innerHeight;
  camera.updateProjectionMatrix();
});

scene.add(new THREE.AmbientLight(0x334455));
const pl=new THREE.PointLight(0x4488ff,1.2,20);
pl.position.set(2,3,2);
scene.add(pl);
const pl2=new THREE.PointLight(0x002244,0.5,10);
pl2.position.set(-2,-1,-2);
scene.add(pl2);

let frame=0;
function animate(){
  requestAnimationFrame(animate);
  frame++;
  // sensor is static
  pl.intensity=0.3+0.2*Math.sin(frame*0.05);
  // dots are persistent
  renderer.render(scene,camera);
}
animate();
</script>
</body>
</html>"""

targets_data = {}
targets_lock = threading.Lock()
clients      = set()

FRAME_SIZES = {0x10: 25, 0x18: 30}

def serial_reader():
    ser = serial.Serial(PORT_SERIAL, BAUD, timeout=0.05)
    ser.reset_input_buffer()
    buf = bytearray()
    last_seq = -1
    while True:
        chunk = ser.read(256)
        if chunk:
            buf.extend(chunk)
        while len(buf) >= 5:
            if buf[0] != 0x01:
                del buf[0]; continue
            ftype = buf[4]
            if ftype not in FRAME_SIZES:
                del buf[0]; continue
            flen = FRAME_SIZES[ftype]
            if len(buf) < flen: break
            frame = bytes(buf[:flen])
            del buf[:flen]
            seq = struct.unpack_from('<H', frame, 1)[0]
            if seq == last_seq: continue
            last_seq = seq
            if ftype == 0x18 and frame[5] == 0x0A:
                x   = struct.unpack_from('<f', frame, 12)[0]
                y   = struct.unpack_from('<f', frame, 16)[0]
                z   = struct.unpack_from('<f', frame, 20)[0]
                vel = struct.unpack_from('<i', frame, 24)[0]
                tid = 0
                rng = math.sqrt(x**2+y**2+z**2)
                with targets_lock:
                    targets_data[tid] = {
                        'tid':tid,'x':round(x,4),'y':round(y,4),
                        'z':round(z,4),'vel':vel,'rng':round(rng,4),
                        'ts':time.time()}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','text/html')
        self.end_headers()
        self.wfile.write(HTML.encode())
    def log_message(self,*a): pass

async def ws_handler(ws, path=None):
    clients.add(ws)
    try:    await ws.wait_closed()
    finally: clients.discard(ws)

async def broadcast_loop():
    global clients
    while True:
        await asyncio.sleep(0.05)
        if not clients:
            continue
        now = time.time()
        with targets_lock:
            live = [v for v in targets_data.values() if now-v['ts']<2.0]
        msg = json.dumps({'type':'targets','targets':live})
        dead = set()
        for ws in list(clients):
            try:    await ws.send(msg)
            except: dead.add(ws)
        clients -= dead

async def main_async():
    async with websockets.serve(ws_handler, 'localhost', WS_PORT):
        await broadcast_loop()

def main():
    threading.Thread(target=serial_reader, daemon=True).start()
    threading.Thread(
        target=lambda: HTTPServer(('localhost',HTTP_PORT),Handler).serve_forever(),
        daemon=True).start()
    print(f"CTW 3D Radar — open http://localhost:{HTTP_PORT}")
    asyncio.run(main_async())

if __name__ == '__main__':
    main()
