#!/usr/bin/env python3
"""make_cover.py — generate a cover/thumbnail for a clip.

Grabs a frame from the video and overlays the hook text. Output is a PNG next to the
clip (or --out). Run it yourself, or have Codex run/restyle it — the layout, fonts, and
colors below are easy to tweak.

    python3 make_cover.py <video> "YOUR HOOK TEXT" [--out cover.png] [--time 1.0] [--pos bottom]
"""
import argparse
import subprocess
import sys
import textwrap
from pathlib import Path

FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",   # Fedora
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",     # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def grab_frame(video, t, out):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", str(t), "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)


def find_font():
    for c in FONT_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def main():
    ap = argparse.ArgumentParser(description="Make a cover image from a clip + hook text.")
    ap.add_argument("video")
    ap.add_argument("hook")
    ap.add_argument("--out")
    ap.add_argument("--time", type=float, default=1.0, help="seconds into the clip to grab")
    ap.add_argument("--pos", choices=["top", "center", "bottom"], default="bottom")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"not found: {video}")
    out = Path(args.out) if args.out else video.with_name(video.stem + "_cover.png")

    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        sys.exit("install Pillow:  .venv/bin/pip install pillow")

    tmp = out.with_suffix(".frame.png")
    grab_frame(video, args.time, tmp)
    img = Image.open(tmp).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    fp = find_font()
    fs = max(30, W // 12)
    font = ImageFont.truetype(fp, fs) if fp else ImageFont.load_default()

    chars_per_line = max(10, int(W / (fs * 0.58)))
    lines = textwrap.wrap(args.hook.upper(), width=chars_per_line) or [""]
    lh = int(fs * 1.2)
    block_h = lh * len(lines)
    pad = int(fs * 0.4)

    if args.pos == "top":
        y0 = int(H * 0.06)
    elif args.pos == "center":
        y0 = (H - block_h) // 2
    else:
        y0 = H - block_h - int(H * 0.08)

    # legibility strip behind the text
    draw.rectangle([0, y0 - pad, W, y0 + block_h + pad], fill=(0, 0, 0, 120))

    y = y0
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        x = (W - tw) // 2
        for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2)]:   # outline
            draw.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += lh

    img.save(out)
    try:
        tmp.unlink()
    except OSError:
        pass
    print(f"wrote {out}  ({W}x{H})")


if __name__ == "__main__":
    main()
