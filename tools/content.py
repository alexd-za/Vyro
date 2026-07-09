#!/usr/bin/env python3
"""content.py — draft the words that ship with a clip.

Generates a prefilled content sheet from the campaign brief: title options,
caption options, a pinned author comment (with an engagement question),
and the exact hashtag line — all checked against the brief's banned phrases.

    content.py <clip.mp4> --brief briefs/x.json --hook "what the clip shows"
               [--out clip.content.md]

These are DRAFTS in the campaign's voice for the agent/user to polish —
the two "your captions" slots stay yours. Never invents claims: everything
is built from the brief + the hook you pass (which step 5 of the playbook
already verified against the footage).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def banned_hits(texts, banned):
    hits = set()
    for t in texts:
        low = t.lower()
        hits |= {p for p in banned if p.lower() in low}
    return sorted(hits)


def main():
    ap = argparse.ArgumentParser(description="Draft titles/captions/pinned comment for a clip.")
    ap.add_argument("clip")
    ap.add_argument("--brief", required=True)
    ap.add_argument("--hook", required=True,
                    help="one honest line about what the footage shows")
    ap.add_argument("--out")
    args = ap.parse_args()

    clip = Path(args.clip)
    brief = json.loads(Path(args.brief).read_text())
    campaign = brief.get("campaign", clip.parent.name)
    tags = " ".join(brief.get("required_hashtags", []))
    disclosure = brief.get("disclosure", "")
    banned = brief.get("banned_phrases", [])
    hook = args.hook.strip().rstrip(".")
    dur = duration_of(clip)

    titles = [
        f"{hook} 🤯 {tags}",
        f"POV: {hook[0].lower()}{hook[1:]} {tags}",
        f"{hook} — wait for it 👀 {tags}",
    ]
    captions = [
        f"{hook}. {disclosure} {tags}".replace("  ", " ").strip(),
        f"Still thinking about this one — {hook[0].lower()}{hook[1:]} {disclosure} {tags}".strip(),
        f"{hook} 🔥 which part surprised you? {disclosure} {tags}".strip(),
    ]
    pinned = [
        f"{hook} — what would YOU have done? 👇 {tags}",
        f"Watch till the end 👀 favorite moment? 👇 {tags}",
        f"Real question: did you spot it? 👇 {tags}",
    ]

    problems = banned_hits(titles + captions + pinned, banned)
    if problems:
        sys.exit(f"drafts hit banned phrases {problems} — rewrite the --hook and retry")

    out = Path(args.out) if args.out else clip.with_suffix("").with_suffix("") \
        .with_name(clip.stem + ".content.md")
    lines = [
        f"# Content sheet — {clip.name}",
        f"_campaign: {campaign} · length: {dur:.1f}s_" if dur else f"_campaign: {campaign}_",
        "",
        "## Hook  (on screen, first 1–2 seconds)",
        hook,
        "",
        "## Title options  (drafts — polish in the campaign's voice)",
        *[f"{i}. {t}" for i, t in enumerate(titles, 1)],
        "",
        "## Caption options",
        *[f"{i}. {c}" for i, c in enumerate(captions, 1)],
        "",
        "## Pinned author comment  (post it, then pin it — drives replies)",
        *[f"{i}. {p}" for i, p in enumerate(pinned, 1)],
        "",
        "## Hashtags  (required by the brief — do not drop these)",
        tags or "(brief has none — double-check that)",
        "",
        "## Sound / music",
        "**Suggested vibe:** <fill in — trending sounds must be added in the app>",
        "",
        "## Cover",
        f"**Generate:** `./clip cover {clip} \"{hook.upper()}\"`",
        "",
        "## Cover image prompt  (for your image model — GPT-image, Imagen, etc.)",
        f"Design a 1080x1920 (9:16) short-form video cover for the \"{campaign}\" campaign. "
        f"Bold condensed title text reading \"{hook.upper()}\" in white with a thick dark "
        f"outline, accent color {brief.get('brand_color', '#FF6A2C')}. High contrast, clean, "
        "scroll-stopping, tasteful — no clutter. Do not include any health or efficacy "
        "claims, and no logos or watermarks that are not part of this campaign.",
        "",
        "## Your captions  (left blank on purpose — these two are yours)",
        "- [ ] My caption 1:",
        "- [ ] My caption 2:",
        "",
        "> Drafts checked against the brief's banned_phrases. Remember: likes/comments ON, "
        "no boosting/paid promotion of the post.",
    ]
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
