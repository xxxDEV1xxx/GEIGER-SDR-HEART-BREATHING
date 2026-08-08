using System;
using System.Collections.Generic;
using System.Linq;

namespace GeigerScope
{
    public record WavePoint(int Index, double Dr, double X, double Y);

    public record SlopeSegment(
        WavePoint From, WavePoint To,
        double VisualAngleDeg, double DeltaDr, double DeltaSec,
        bool IsRise);

    public record WaveformAnalysis(
        List<WavePoint>    Peaks,
        List<WavePoint>    Troughs,
        List<SlopeSegment> Slopes,
        string             PatternName,
        string             PatternDetail,
        double             EstPeriodSec,
        double             EstFreqHz,
        double             RiseFallRatio);

    public static class WaveformAnalyzer
    {
        private const double MIN_PROMINENCE = 0.012;

        public static WaveformAnalysis Analyze(
            double[]      drSmooth,
            SamplePoint[] samples,
            double xStep, double h, double yMax)
        {
            if (drSmooth.Length < 6)
                return Empty();

            var peaks   = DetectExtrema(drSmooth, xStep, h, yMax, true);
            var troughs = DetectExtrema(drSmooth, xStep, h, yMax, false);

            var slopes = BuildSlopes(peaks, troughs, drSmooth,
                                     samples, xStep, h, yMax);

            double period   = EstimatePeriod(peaks, samples);
            double freqHz   = period > 0 ? 1.0 / period : 0;
            double rfRatio  = RiseFallRatio(slopes);
            var (name, detail) = Classify(period, freqHz, rfRatio,
                                          slopes, drSmooth);

            return new WaveformAnalysis(
                peaks, troughs, slopes,
                name, detail, period, freqHz, rfRatio);
        }

        // ── Extrema detection ─────────────────────────────────────────────
        private static List<WavePoint> DetectExtrema(
            double[] dr, double xStep, double h, double yMax, bool peak)
        {
            var result = new List<WavePoint>();
            int w = Math.Max(4, dr.Length / 25);

            for (int i = w; i < dr.Length - w; i++)
            {
                bool ok = true;
                for (int j = i - w; j <= i + w; j++)
                {
                    if (j == i) continue;
                    if (peak  && dr[j] >= dr[i]) { ok = false; break; }
                    if (!peak && dr[j] <= dr[i]) { ok = false; break; }
                }
                if (!ok) continue;

                // Prominence filter for peaks only
                if (peak)
                {
                    int span = Math.Min(w * 4, dr.Length);
                    double leftMin  = dr[Math.Max(0, i - span)..i].Min();
                    double rightMin = dr[i..Math.Min(dr.Length, i + span)].Min();
                    if (dr[i] - Math.Max(leftMin, rightMin) < MIN_PROMINENCE)
                        continue;
                }

                double x = i * xStep;
                double y = h - (dr[i] / yMax) * h;
                result.Add(new WavePoint(i, dr[i], x, y));
            }
            return result;
        }

        // ── Build rise/fall segments ──────────────────────────────────────
        private static List<SlopeSegment> BuildSlopes(
            List<WavePoint> peaks, List<WavePoint> troughs,
            double[] dr, SamplePoint[] samples,
            double xStep, double h, double yMax)
        {
            var all    = peaks.Concat(troughs)
                              .OrderBy(p => p.Index).ToList();
            var result = new List<SlopeSegment>();

            for (int i = 0; i < all.Count - 1; i++)
            {
                var a = all[i];
                var b = all[i + 1];
                bool isRise = b.Dr > a.Dr;

                // Visual angle: positive = rising on screen
                double dx = b.X - a.X;
                double dy = a.Y - b.Y;   // canvas Y inverted
                double angle = Math.Atan2(dy, dx) * 180.0 / Math.PI;

                double deltaDr  = b.Dr - a.Dr;
                double deltaSec = 0;
                if (a.Index < samples.Length && b.Index < samples.Length)
                    deltaSec = (samples[b.Index].WallNs
                              - samples[a.Index].WallNs) / 1e9;

                result.Add(new SlopeSegment(a, b, angle,
                                            deltaDr, deltaSec, isRise));
            }
            return result;
        }

        // ── Period estimate ───────────────────────────────────────────────
        private static double EstimatePeriod(
            List<WavePoint> peaks, SamplePoint[] samples)
        {
            if (peaks.Count < 2) return 0;
            var intervals = new List<double>();
            for (int i = 1; i < peaks.Count; i++)
            {
                int a = peaks[i - 1].Index, b = peaks[i].Index;
                if (a < samples.Length && b < samples.Length)
                    intervals.Add(
                        (samples[b].WallNs - samples[a].WallNs) / 1e9);
            }
            return intervals.Count > 0 ? intervals.Average() : 0;
        }

        // ── Rise/fall ratio ───────────────────────────────────────────────
        private static double RiseFallRatio(List<SlopeSegment> slopes)
        {
            var rises = slopes.Where(s =>  s.IsRise).ToList();
            var falls = slopes.Where(s => !s.IsRise).ToList();
            if (rises.Count == 0 || falls.Count == 0) return 1.0;
            double r = rises.Average(s => s.DeltaSec);
            double f = falls.Average(s => Math.Abs(s.DeltaSec));
            return f > 0 ? r / f : 1.0;
        }

        // ── Pattern classifier ────────────────────────────────────────────
        private static (string name, string detail) Classify(
            double period, double freqHz, double rfRatio,
            List<SlopeSegment> slopes, double[] dr)
        {
            if (slopes.Count == 0)
                return ("ACCUMULATING", "Insufficient waveform data.");

            string band = freqHz switch
            {
                0           => "UNDETERMINED",
                < 0.003     => $"ULF  {freqHz:0.0000} Hz  (Ultra Low Frequency < 0.003)",
                < 0.03      => $"ELF  {freqHz:0.0000} Hz  (Extremely Low Frequency)",
                < 0.3       => $"SLF  {freqHz:0.000} Hz  (Super Low Frequency)",
                < 3.0       => $"LF   {freqHz:0.00} Hz  (Low Frequency)",
                _           => $"MF+  {freqHz:0.0} Hz  (exceeds Nyquist for 1Hz sampling)"
            };

            string shape = rfRatio switch
            {
                > 0 and < 0.3   => "SAWTOOTH — fast-rise / slow-fall  (pulsed RF leading edge)",
                >= 0.3 and < 0.8 => "ASYMMETRIC FALL — modulated carrier, trailing dominance",
                >= 0.8 and < 1.2 => "SINUSOIDAL — symmetric rise/fall  (continuous wave CW)",
                >= 1.2 and < 3.0 => "ASYMMETRIC RISE — slow-rise carrier, AM-modulated",
                >= 3.0           => "REVERSE SAWTOOTH — capacitive discharge / trailing pulse",
                _                => "UNCLASSIFIED"
            };

            // Variance check
            double mean = dr.Average();
            double var  = dr.Average(v => (v - mean) * (v - mean));
            string reg  = var < 0.0003
                ? "HIGH REGULARITY — structured source indicated"
                : var < 0.002
                ? "MODERATE VARIABILITY — possible multi-source"
                : "HIGH VARIABILITY — broadband or noise-dominant";

            string name = shape.Split('—')[0].Trim();
            string detail = $"{shape}\n{band}\n{reg}" +
                            (period > 0
                                ? $"\nEst period {period:0.0}s  |  R/F {rfRatio:0.00}  |  f={freqHz:0.0000} Hz"
                                : "");
            return (name, detail);
        }

        private static WaveformAnalysis Empty() =>
            new(new(), new(), new(),
                "ACCUMULATING", "Need more data.", 0, 0, 1);
    }
}