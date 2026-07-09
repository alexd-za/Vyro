#!/usr/bin/env python3
"""
find_moments.py — find candidate short-form clips inside a longer source video.

Signals used (all via ffmpeg, no heavy ML required):
  * audio loudness over time (ebur128 momentary)  -> excitement / emphasis
  * silence intervals (silencedetect)             -> clean clip start/end points
  * scene changes (scene score)                   -> visual hooks / cuts

Output: candidates.json + candidates.md, ranked, each with in/out timestamps,
a score, and why it was picked. Optional --transcribe attaches spoken text
(if faster-whisper or openai-whisper is installed) so an agent can judge hooks
by content.

This is a FIRST PASS that saves you scrubbing footage. Always apply the
campaign brief + your own judgement to the shortlist before cutting.
"""

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

FLOOR_LUFS = -70.0  # treat -inf / true silence as this


def run(cmd):
    """Run a command, return combined stderr+stdout text (ffmpeg logs to stderr)."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.stdout or ""


def probe(path):
    """Return (duration_seconds, has_audio)."""
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "format=duration:stream=codec_type", "-of", "json", path])
    duration, has_audio = 0.0, False
    try:
        data = json.loads(out)
        duration = float(data.get("format", {}).get("duration", 0.0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                has_audio = True
    except Exception:
        pass
    return duration, has_audio


def loudness_series(path):
    """List of (t, rms_db) loudness samples via per-window RMS (ffmpeg astats).

    ~0.5s windows; louder = closer to 0 dB. ebur128's momentary output isn't
    reliably parseable across ffmpeg builds, so we use astats RMS instead.
    """
    log = run(["ffmpeg", "-nostats", "-i", path, "-af",
               "aresample=16000,asetnsamples=8000,"
               "astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level",
               "-f", "null", "-"])
    series, last_t = [], None
    for line in log.splitlines():
        mt = re.search(r"pts_time:\s*([0-9.]+)", line)
        if mt:
            last_t = float(mt.group(1))
            continue
        mv = re.search(r"RMS_level=\s*(-?inf|-?[0-9.]+)", line)
        if mv and last_t is not None:
            val = FLOOR_LUFS if "inf" in mv.group(1) else float(mv.group(1))
            series.append((last_t, val))
            last_t = None
    return series


def silences(path, noise_db=-30, min_dur=0.3):
    """List of (start, end) silent intervals."""
    log = run(["ffmpeg", "-i", path, "-af",
               f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    starts, out = [], []
    for line in log.splitlines():
        s = re.search(r"silence_start:\s*([0-9.]+)", line)
        e = re.search(r"silence_end:\s*([0-9.]+)", line)
        if s:
            starts.append(float(s.group(1)))
        elif e and starts:
            out.append((starts.pop(), float(e.group(1))))
    return out


def scene_changes(path, thresh=0.3):
    """List of timestamps where a scene change was detected."""
    log = run(["ffmpeg", "-i", path, "-filter_complex",
               f"select='gt(scene,{thresh})',metadata=print", "-an", "-f", "null", "-"])
    return [float(m.group(1)) for m in re.finditer(r"pts_time:\s*([0-9.]+)", log)]


# ---------- scoring helpers ----------

def to_grid(series, duration, step=0.2):
    """Resample (t,lufs) samples onto a uniform grid via nearest-neighbour hold."""
    n = max(1, int(duration / step) + 1)
    grid = [FLOOR_LUFS] * n
    if not series:
        return grid, step
    si = 0
    for i in range(n):
        t = i * step
        while si + 1 < len(series) and series[si + 1][0] <= t:
            si += 1
        grid[i] = series[si][1]
    return grid, step


def smooth(values, radius):
    if radius <= 0:
        return values[:]
    out = []
    n = len(values)
    for i in range(n):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def nearest(points, t, tol):
    best, bd = None, tol
    for p in points:
        d = abs(p - t)
        if d <= bd:
            best, bd = p, d
    return best


def find_peaks(sm, step, min_gap_s=4.0):
    """Indices of local maxima, spaced at least min_gap_s apart, above the median."""
    n = len(sm)
    if n < 3:
        return [0] if n else []
    med = sorted(sm)[n // 2]
    k = max(1, int(1.0 / step))  # ~1s neighbourhood
    raw = []
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        if sm[i] >= max(sm[lo:hi]) and sm[i] > med:
            raw.append(i)
    # enforce spacing, keep the loudest in each cluster
    peaks, gap = [], int(min_gap_s / step)
    for i in raw:
        if peaks and i - peaks[-1] < gap:
            if sm[i] > sm[peaks[-1]]:
                peaks[-1] = i
        else:
            peaks.append(i)
    return peaks


def main():
    ap = argparse.ArgumentParser(description="Find candidate clips in a source video.")
    ap.add_argument("video", help="path to source video")
    ap.add_argument("--target", type=float, default=22.0, help="target clip length (s)")
    ap.add_argument("--min", type=float, default=8.0, help="min clip length (s)")
    ap.add_argument("--max", type=float, default=45.0, help="max clip length (s)")
    ap.add_argument("--count", type=int, default=12, help="how many candidates to return")
    ap.add_argument("--scene-thresh", type=float, default=0.3)
    ap.add_argument("--outdir", default=".", help="where to write candidates.json/.md")
    ap.add_argument("--transcribe", action="store_true",
                    help="attach spoken text per candidate (needs whisper installed)")
    args = ap.parse_args()

    path = args.video
    if not Path(path).exists():
        sys.exit(f"file not found: {path}")

    duration, has_audio = probe(path)
    if duration <= 0:
        sys.exit("could not read video duration")
    print(f"duration={duration:.1f}s  audio={'yes' if has_audio else 'no'}", file=sys.stderr)

    scenes = scene_changes(path, args.scene_thresh)
    sils = silences(path) if has_audio else []
    sil_edges = sorted([e for pair in sils for e in pair])

    if has_audio:
        grid, step = to_grid(loudness_series(path), duration)
    else:
        grid, step = [FLOOR_LUFS] * (int(duration / 0.2) + 1), 0.2

    sm = smooth(grid, radius=int(1.0 / step))
    lo, hi = min(sm), max(sm)
    span = (hi - lo) or 1.0

    def norm(v):
        return (v - lo) / span

    # --- generate candidate centres ---
    if has_audio and hi > lo + 1.0:
        centres = [i * step for i in find_peaks(sm, step)]
    else:
        # no useful audio -> seed on scene changes, then fill with uniform samples
        centres = list(scenes)
        t = args.target / 2
        while t < duration:
            centres.append(t)
            t += args.target
    if not centres:
        centres = [duration / 2]

    # --- build + score windows ---
    cands = []
    for c in centres:
        length = min(args.max, max(args.min, args.target))
        start = c - 0.30 * length          # put the peak ~30% in (hook early)
        end = start + length
        # snap to clean edges in silence
        if sil_edges:
            ss = nearest(sil_edges, start, 2.0)
            ee = nearest(sil_edges, end, 2.0)
            if ss is not None:
                start = ss
            if ee is not None and ee > start + args.min:
                end = ee
        start = max(0.0, start)
        end = min(duration, end)
        if end - start < args.min:
            continue

        i0, i1 = int(start / step), max(int(start / step) + 1, int(end / step))
        seg = sm[i0:i1] or [lo]
        mean_l = sum(seg) / len(seg)
        var_l = (sum((x - mean_l) ** 2 for x in seg) / len(seg)) ** 0.5
        scene_hook = any(start <= s <= start + 0.25 * (end - start) for s in scenes)
        scene_n = sum(1 for s in scenes if start <= s <= end)
        clean_start = nearest(sil_edges, start, 0.5) is not None if sil_edges else False

        score = (norm(mean_l) * 1.0
                 + min(var_l / span, 1.0) * 0.4
                 + (0.25 if scene_hook else 0.0)
                 + (0.10 if clean_start else 0.0))

        cands.append({
            "start": round(start, 2), "end": round(end, 2),
            "duration": round(end - start, 2), "score": round(score, 3),
            "scene_cuts": scene_n,
            "reasons": [r for r in [
                "loud/energetic moment" if norm(mean_l) > 0.5 else None,
                "dynamic (big loudness swing)" if var_l / span > 0.25 else None,
                "visual hook near start" if scene_hook else None,
                "clean start on silence" if clean_start else None,
            ] if r],
        })

    # --- dedupe overlapping, keep best ---
    cands.sort(key=lambda x: x["score"], reverse=True)
    chosen = []
    for c in cands:
        clash = False
        for k in chosen:
            ov = min(c["end"], k["end"]) - max(c["start"], k["start"])
            if ov > 0.4 * c["duration"]:
                clash = True
                break
        if not clash:
            chosen.append(c)
        if len(chosen) >= args.count:
            break

    chosen.sort(key=lambda x: x["start"])  # present in timeline order
    for i, c in enumerate(chosen, 1):
        c["rank"] = i

    if args.transcribe:
        attach_transcript(path, chosen)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "candidates.json").write_text(json.dumps(chosen, indent=2))
    (outdir / "candidates.md").write_text(render_md(path, duration, chosen))
    print(f"wrote {len(chosen)} candidates -> {outdir/'candidates.md'}", file=sys.stderr)
    print(json.dumps(chosen, indent=2))


def attach_transcript(path, cands):
    try:
        from faster_whisper import WhisperModel
    except Exception:
        print("(--transcribe: faster-whisper not installed; skipping text)", file=sys.stderr)
        return
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = model.transcribe(path)
    words = [(s.start, s.end, s.text.strip()) for s in segs]
    for c in cands:
        txt = " ".join(t for (a, b, t) in words if b > c["start"] and a < c["end"])
        c["text"] = txt.strip()[:400]


def ts(sec):
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def render_md(path, duration, cands):
    out = [f"# Clip candidates — `{Path(path).name}`",
           f"_source length {ts(duration)} · {len(cands)} candidates, timeline order_\n"]
    for c in cands:
        out.append(f"## {c['rank']}. {ts(c['start'])} → {ts(c['end'])}  "
                   f"({c['duration']:.0f}s · score {c['score']})")
        if c.get("reasons"):
            out.append("- " + "; ".join(c["reasons"]))
        if c.get("text"):
            out.append(f"- transcript: {c['text']}")
        # re-encode on purpose: -c copy snaps to keyframes and lands seconds off
        out.append(f"- cut command: "
                   f"`./clip cut \"{path}\" {c['start']} {c['end']} "
                   f"clip_{c['rank']:02d}.mp4`\n")
    out.append("> First pass only. Check each against the campaign brief and your own taste "
               "before cutting — pick the strongest 8–15, not all of them.")
    return "\n".join(out)


if __name__ == "__main__":
    main()
