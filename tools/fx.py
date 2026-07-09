#!/usr/bin/env python3
"""fx.py — visual resources so edits don't look bland.

  fonts [--list]     download a curated set of open-licensed (OFL) display fonts
                     from the Google Fonts repo into assets/fonts/ (no API key).
                     ./clip produce auto-uses Anton for hook titles when present.

Only pulls from github.com/google/fonts (SIL OFL / Apache-2.0 — free for
commercial use, embedding, and video). No logos, no third-party campaign
content — those stay banned by the campaign rules.
"""
import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "assets" / "fonts"
BASE = "https://raw.githubusercontent.com/google/fonts/main"
FONTS = {  # family -> repo path (license is the first path segment)
    "Anton":         "ofl/anton/Anton-Regular.ttf",          # heavy condensed — hooks
    "Bebas Neue":    "ofl/bebasneue/BebasNeue-Regular.ttf",  # tall condensed — titles
    "Archivo Black": "ofl/archivoblack/ArchivoBlack-Regular.ttf",   # chunky — covers
    "Luckiest Guy":  "apache/luckiestguy/LuckiestGuy-Regular.ttf",  # fun — casual clips
}


def cmd_fonts(a):
    if a.list:
        for name, path in FONTS.items():
            f = FONT_DIR / Path(path).name
            print(f"  {'[ok]' if f.exists() else '[--]'} {name:<14} {f}")
        return
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    got = 0
    for name, path in FONTS.items():
        dest = FONT_DIR / Path(path).name
        if dest.exists():
            print(f"  · {name} already present")
            continue
        url = f"{BASE}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            if not data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                print(f"  ! {name}: unexpected file format, skipped", file=sys.stderr)
                continue
            dest.write_bytes(data)
            print(f"  + {name}  ({len(data)//1024} KB, {path.split('/')[0]})")
            got += 1
        except OSError as e:
            print(f"  ! {name}: download failed ({e})", file=sys.stderr)
    print(f"\n{got} new font(s) in {FONT_DIR.relative_to(ROOT)}/ — "
          "./clip produce now uses Anton for hook titles automatically.")


def main():
    ap = argparse.ArgumentParser(description="Visual resources (open-licensed).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fonts", help="fetch curated OFL display fonts")
    p.add_argument("--list", action="store_true", help="show status, download nothing")
    p.set_defaults(func=cmd_fonts)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
