#!/usr/bin/env python3
"""
produce.py — turn a raw cut into a FINISHED vertical clip.

What a raw ./clip cut is missing before it can earn views:
  * 9:16 reframe (crop = full-frame punch, or blur = letterbox on blurred fill)
  * slow push-in (Ken Burns) so the frame is never static
  * color grade + vignette
  * word-synced animated captions (whisper), brand keywords highlighted
  * optional hook title for the first seconds
  * loudness normalization + fades

    python3 produce.py <cut.mp4> [--brief briefs/x.json] [--hook "LINE1\\nLINE2"]
                       [--reframe crop|blur] [--grade vibrant|moody|none]
                       [--no-captions] [--out out.mp4]

Brief fields used (all optional): brand_color ("#E8551F"), highlight_words
(["motrin","recharge"]). Captions are skipped gracefully if no whisper backend
or no speech — the clip still renders.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FONTS_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu-sans-fonts",
              "/usr/share/fonts/dejavu"]
W, H = 1080, 1920


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)


def duration_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)], stdout=subprocess.PIPE).stdout
    try:
        return float(out.strip())
    except ValueError:
        sys.exit(f"could not read duration: {path}")


def transcribe_words(path):
    """[{'w','s','e'}] via faster-whisper, else openai-whisper, else None."""
    try:
        from faster_whisper import WhisperModel
        segs, _ = WhisperModel("base", device="cpu", compute_type="int8") \
            .transcribe(str(path), word_timestamps=True)
        return [{"w": w.word.strip(), "s": w.start, "e": w.end}
                for s in segs for w in (s.words or [])]
    except ImportError:
        pass
    try:
        import whisper
        r = whisper.load_model("base").transcribe(str(path), word_timestamps=True, fp16=False)
        return [{"w": w["word"].strip(), "s": w["start"], "e": w["end"]}
                for seg in r["segments"] for w in seg.get("words", [])]
    except ImportError:
        return None


def hex_to_ass(color):
    """'#E8551F' -> ASS '&H001F55E8&' (BGR)."""
    c = color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return r"&H002C6AFF&"
    r_, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H00{b}{g}{r_}&".upper()


def ass_time(s):
    cs = int(round(max(0.0, s) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def build_ass(words, hook, highlight, brand_ass, out_path):
    """Pop-in caption chunks (<=3 words) + optional hook title. Returns event count."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,DejaVu Sans,60,&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,5,3,5,40,40,40,1\n"
        "Style: Hook,DejaVu Sans,60,&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,6,3,5,60,60,60,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n")
    lines = []
    if hook:
        fx = (r"{\pos(540,470)\an5\fad(150,200)\fscx60\fscy60"
              r"\t(0,200,\fscx104\fscy104)\t(200,320,\fscx100\fscy100)}")
        lines.append(f"Dialogue: 0,{ass_time(0.1)},{ass_time(2.8)},Hook,,0,0,0,,"
                     f"{fx}{hook}")
    chunk = []
    def flush():
        if not chunk:
            return
        s, e = chunk[0]["s"], max(chunk[-1]["e"], chunk[0]["s"] + 0.32)
        txt = r"\h".join(
            (brand_ass if w["w"].strip(".,?!").lower() in highlight else r"{\c&H00FFFFFF&}")
            + w["w"] for w in chunk)
        fx = (r"{\pos(540,1340)\an5\fad(70,70)\fscx72\fscy72"
              r"\t(0,120,\fscx106\fscy106)\t(120,200,\fscx100\fscy100)}")
        lines.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{fx}{txt}")
        chunk.clear()
    for w in words:
        if not w["w"]:
            continue
        chunk.append(w)
        if len(chunk) >= 3 or w["w"][-1:] in ".?!,":
            flush()
    flush()
    Path(out_path).write_text(header + "\n".join(lines) + "\n")
    return len(lines)


GRADES = {
    "vibrant": "eq=contrast=1.06:saturation=1.22:brightness=0.01,vignette=PI/5",
    "moody": "eq=contrast=1.12:saturation=1.08:brightness=-0.035,"
             "curves=b='0/0 0.5/0.56 1/1',vignette=PI/4",
    "none": None,
}


def video_filter(reframe, grade, ass_path, fontsdir):
    fill = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if reframe == "blur":
        chain = (f"[0:v]split=2[bg][fg];[bg]{fill},boxblur=22:4,"
                 f"eq=brightness=-0.07[bg2];[fg]scale={W}:-2[fg2];"
                 f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2")
    else:
        chain = f"[0:v]{fill}"
    chain += (f",zoompan=z='min(1.0+0.00018*in,1.07)':x='iw/2-(iw/zoom/2)':"
              f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps=30")
    if GRADES.get(grade):
        chain += "," + GRADES[grade]
    chain += ",fade=t=in:st=0:d=0.5"
    if ass_path:
        chain += f",subtitles={ass_path}"
        if fontsdir:
            chain += f":fontsdir={fontsdir}"
    return chain + "[v]"


def main():
    ap = argparse.ArgumentParser(description="Raw cut -> finished vertical clip.")
    ap.add_argument("video")
    ap.add_argument("--brief", help="campaign brief JSON (brand_color, highlight_words)")
    ap.add_argument("--hook", default="", help=r"on-screen title; \n for a second line")
    ap.add_argument("--reframe", choices=["crop", "blur"], default="crop")
    ap.add_argument("--grade", choices=sorted(GRADES), default="vibrant")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    src = Path(args.video)
    if not src.exists():
        sys.exit(f"not found: {src}")
    out = Path(args.out) if args.out else src.with_name(src.stem + "_final.mp4")
    brief = json.loads(Path(args.brief).read_text()) if args.brief else {}
    highlight = {w.lower() for w in brief.get("highlight_words", [])}
    brand_ass = r"{\c" + hex_to_ass(brief.get("brand_color", "#FF6A2C")) + "}"
    dur = duration_of(src)

    ass_path = None
    if not args.no_captions or args.hook:
        words = [] if args.no_captions else (transcribe_words(src) or [])
        if not args.no_captions and not words:
            print("(no whisper backend or no speech — rendering without captions)",
                  file=sys.stderr)
        if words or args.hook:
            tmp = tempfile.NamedTemporaryFile(suffix=".ass", delete=False)
            hook = args.hook.replace("\\n", r"\N")
            n = build_ass(words, hook, highlight, brand_ass, tmp.name)
            ass_path = tmp.name
            print(f"captions: {n} events", file=sys.stderr)

    fontsdir = next((d for d in FONTS_DIRS if Path(d).is_dir()), None)
    vf = video_filter(args.reframe, args.grade, ass_path, fontsdir)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-af", f"afade=t=out:st={max(0.0, dur - 0.7):.2f}:d=0.6,"
                  "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart"]
    if render(cmd, str(out), dur) != 0:
        sys.exit("ffmpeg render failed")
    print(f"wrote {out}  ({W}x{H}, {dur:.1f}s, grade={args.grade}, reframe={args.reframe})")


def render(cmd, out, dur):
    """Run the encode; live progress bar on a TTY, quiet otherwise."""
    if not (sys.stderr.isatty() and dur > 0):
        return run(cmd + [out]).returncode
    p = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats", out],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    bar = 34
    for line in p.stdout:
        if line.startswith("out_time_ms="):
            try:
                frac = min(1.0, int(line.split("=")[1]) / 1e6 / dur)
            except ValueError:
                continue
            n = int(frac * bar)
            print(f"\r  \033[38;5;209mrendering\033[0m "
                  f"[\033[38;5;209m{'█' * n}\033[2m{'░' * (bar - n)}\033[0m] "
                  f"{frac * 100:3.0f}%", end="", file=sys.stderr)
    rc = p.wait()
    print("\r\033[K", end="", file=sys.stderr)
    if rc != 0:
        print(p.stderr.read(), file=sys.stderr)
    return rc


if __name__ == "__main__":
    main()
