using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace GeigerScope
{
    public class VideoRecorder
    {
        private string?  _frameDir;
        private int      _frameIdx;
        private bool     _recording;
        private DateTime _startTime;

        private const int FPS      = 5;
        private const string FFMPEG =
            @"J:\True-Sentinel\ffmpeg.exe";

        public bool      IsRecording => _recording;
        public TimeSpan  Elapsed     =>
            _recording ? DateTime.Now - _startTime : TimeSpan.Zero;
        public string?   LastOutput  { get; private set; }

        public void Start(string outDir)
        {
            _frameDir  = Path.Combine(outDir,
                $"rec_{DateTime.Now:yyyyMMdd_HHmmss}");
            Directory.CreateDirectory(_frameDir);
            _frameIdx  = 0;
            _recording = true;
            _startTime = DateTime.Now;
            LastOutput = null;
        }

        public void CaptureFrame(Window window)
        {
            if (!_recording || _frameDir == null) return;
            try
            {
                // Force layout commit before capture
                window.UpdateLayout();

                var bmp = new RenderTargetBitmap(
                    (int)window.ActualWidth,
                    (int)window.ActualHeight,
                    96, 96, PixelFormats.Pbgra32);
                bmp.Render(window);
                bmp.Freeze();

                var enc = new PngBitmapEncoder();
                enc.Frames.Add(BitmapFrame.Create(bmp));

                string path = Path.Combine(
                    _frameDir, $"f_{_frameIdx++:D6}.png");
                using var fs = new FileStream(path, FileMode.Create);
                enc.Save(fs);
            }
            catch { }
        }

        public async Task<string> StopAsync()
        {
            _recording = false;
            if (_frameDir == null || _frameIdx == 0)
                return "No frames captured.";

            string mp4 = _frameDir + ".mp4";
            string ff  = File.Exists(FFMPEG) ? FFMPEG : "ffmpeg";

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName  = ff,
            Arguments = $"-y -framerate {FPS} " +
            $"-i \"{Path.Combine(_frameDir, "f_%06d.png")}\" " +
            $"-vf \"scale=trunc(iw/2)*2:trunc(ih/2)*2\" " +
            $"-vcodec libx264 -pix_fmt yuv420p \"{mp4}\"",
                    UseShellExecute        = false,
                    CreateNoWindow         = true,
                    RedirectStandardError  = true,
                };
                var proc = Process.Start(psi);
                if (proc != null)
                {
                    using var cts = new CancellationTokenSource(
                        TimeSpan.FromSeconds(120));
                    try
                    {
                        await proc.WaitForExitAsync(cts.Token);
                    }
                    catch (OperationCanceledException)
                    {
                        proc.Kill();
                        LastOutput = _frameDir;
                        return $"FFmpeg timed out. Frames at:\n{_frameDir}";
                    }
                    if (proc.ExitCode == 0)
                    {
                        Directory.Delete(_frameDir, true);
                        LastOutput = mp4;
                        return $"Saved: {mp4}";
                    }
                }
            }
            catch { }

            // FFmpeg not found — keep PNG sequence
            LastOutput = _frameDir;
            return $"FFmpeg unavailable. Frames at:\n{_frameDir}\n" +
                   $"Run: ffmpeg -r {FPS} -i f_%06d.png output.mp4";
        }
    }
}