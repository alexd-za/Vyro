#!/usr/bin/env python3
"""sort.py — sort dropped randomness into named campaign folders, by itself.

  auto [--yes]          group every uncategorized ingested video, propose
                        campaign names derived from filenames + transcripts,
                        then move files, write notes, and create briefs
  hashtags <campaign> [--ask]   suggest hashtags (4 max, required first);
                        --ask also records the brief's required ones and saves
  captions <campaign>   print the ./clip content command per produced clip

Everything is derived from what's actually there (filenames, transcript
snippets) — it never invents claims. Memory-logged so any AI sees what moved.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "knowledge" / "inbox-manifest.json"
STOP = {"final", "copy", "video", "clip", "edit", "export", "img", "vid", "mov",
        "new", "file", "untitled", "render", "out", "cut", "draft", "v1", "v2",
        "v3", "the", "and", "for", "with", "this", "that", "you", "are", "was",
        "have", "has", "its", "it's", "were", "they", "them", "then", "than",
        "his", "her", "our", "your", "their", "from", "into", "what", "when",
        "where", "who", "how", "why", "not", "but", "all", "can", "will",
        "just", "like", "get", "got", "going", "know", "yeah", "okay"}


def mem_log(text, tags=""):
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "memory.py"), "log",
                        text] + (["--tags", tags] if tags else []), timeout=10)
    except Exception:
        pass


def tokens_from_name(name):
    stem = Path(name).stem.lower()
    raw = re.split(r"[-_\s.]+", stem)
    out = []
    for t in raw:
        t = re.sub(r"[^a-z0-9]", "", t)
        if (not t or t in STOP or t.isdigit() or re.fullmatch(r"\d+p", t)
                or re.fullmatch(r"\d{6,}", t) or len(t) < 3):
            continue
        out.append(t)
    return out


def real_transcript(md_text):
    """Transcript body, or '' when it's only the install-whisper placeholder."""
    m = re.search(r"## transcript.*?\n(.+?)(\n##|\Z)", md_text, re.S)
    body = m.group(1).strip() if m else ""
    return "" if body.startswith("_(install") else body


def tokens_from_transcript(vid_id):
    notes = ROOT / "work" / "_unsorted" / vid_id / "notes.md"
    if not notes.exists():
        return []
    body = real_transcript(notes.read_text())
    words = [w for w in re.findall(r"[a-z']{4,}", body.lower())
             if w not in STOP]
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:3]]


def transcript_snippet(vid_id):
    notes = ROOT / "work" / "_unsorted" / vid_id / "notes.md"
    return real_transcript(notes.read_text()) if notes.exists() else ""


def slug_for(entry, vid_id):
    toks = tokens_from_name(entry["name"]) + tokens_from_transcript(vid_id)
    seen, best = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            best.append(t)
        if len(best) == 3:
            break
    return "-".join(best) if best else f"unsorted-{datetime.now():%Y%m%d}"


def load_manifest():
    if not MANIFEST.exists():
        sys.exit("no inbox manifest — run ./clip ingest first")
    return json.loads(MANIFEST.read_text())


def make_brief(slug):
    brief = ROOT / "briefs" / f"{slug}.json"
    if brief.exists():
        return
    brief.parent.mkdir(exist_ok=True)
    brief.write_text(json.dumps({
        "campaign": slug,
        "caption_template": "WRITE A SCROLL-STOPPING FIRST LINE HERE",
        "required_hashtags": [],
        "banned_phrases": [],
        "min_seconds": 0, "max_seconds": 0,
        "brand_color": "#FF6A2C", "highlight_words": slug.split("-"),
        "ai_generated": True, "is_branded": False, "disclosure": "",
        "video_url": "", "platforms": ["tiktok", "reels", "shorts"],
        "notes": f"auto-created by ./clip sort on {datetime.now():%Y-%m-%d}. "
                 "Paste the campaign's real rules (hashtags, disclosures, lengths) here.",
    }, indent=2))


def cmd_auto(a):
    man = load_manifest()
    todo = {vid: e for vid, e in man.items() if not e.get("category")}
    if not todo:
        print("nothing to sort — every ingested video already has a campaign.")
        return
    # propose slugs, then merge groups whose token sets overlap >= 50%
    plan = {vid: slug_for(e, vid) for vid, e in todo.items()}
    slugs = list(dict.fromkeys(plan.values()))
    canon = {}
    for s in slugs:
        st = set(s.split("-"))
        for c in canon:
            ct = set(c.split("-"))
            if len(st & ct) * 2 >= min(len(st), len(ct)) * 1:
                canon[s] = canon[c]
                break
        else:
            canon[s] = s
    plan = {vid: canon.get(s, s) for vid, s in plan.items()}

    print("proposed sorting:")
    for vid, slug in plan.items():
        print(f"  {todo[vid]['name']}  ->  work/{slug}/")
    if not a.yes:
        if input("apply? [y/N] ").strip().lower() != "y":
            print("nothing moved.")
            return

    for vid, slug in plan.items():
        e = man[vid]
        dest_dir = ROOT / "work" / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        (ROOT / "out" / slug).mkdir(parents=True, exist_ok=True)
        staged = Path(e["staged"])
        if not staged.is_absolute():
            staged = ROOT / staged
        dest = dest_dir / staged.name
        if staged.exists() and not dest.exists():
            shutil.move(str(staged), str(dest))
        notes_src = staged.parent / "notes.md"
        if notes_src.exists():
            shutil.move(str(notes_src), str(dest_dir / f"{staged.stem}.notes.md"))
        shutil.rmtree(staged.parent, ignore_errors=True)
        make_brief(slug)
        cn = dest_dir / "CAMPAIGN-NOTES.md"
        if not cn.exists():
            cn.write_text(f"# {slug} — campaign notes\n\n")
        dur = e.get("duration")
        with cn.open("a") as f:
            f.write(f"- {datetime.now():%Y-%m-%d} added `{staged.name}`"
                    f"{f' ({dur:.0f}s)' if dur else ''} — grouped by: "
                    f"{', '.join(slug.split('-'))}\n")
        e["category"] = slug
        e["staged"] = str(dest)
        mem_log(f"sorted {staged.name} -> {slug}", "stage=sort")
        print(f"  moved {staged.name} -> work/{slug}/")
    MANIFEST.write_text(json.dumps(man, indent=2))
    print(f"\nsorted {len(plan)} video(s). Next: ./clip hashtags <campaign> --ask")


def suggest_hashtags(slug, required):
    # required first and never dropped; topical suggestions fill up to 4 total
    tags = []
    for r in required:
        r = "#" + r.lstrip("#")
        if r.lower() not in (t.lower() for t in tags):
            tags.append(r)
    topics = slug.split("-")
    for vid_file in sorted((ROOT / "work" / slug).glob("*.notes.md")):
        body = real_transcript(vid_file.read_text())
        if body:
            words = [w for w in re.findall(r"[a-z']{4,}", body.lower())
                     if w not in STOP]
            freq = {}
            for w in words:
                freq[w] = freq.get(w, 0) + 1
            topics += [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:2]]
    for t in topics:
        if len(tags) >= 4:
            break
        tag = "#" + re.sub(r"[^a-z0-9]", "", t)
        if len(tag) > 2 and tag.lower() not in (x.lower() for x in tags):
            tags.append(tag)
    return tags[:4]


def cmd_hashtags(a):
    brief_path = ROOT / "briefs" / f"{a.campaign}.json"
    if not brief_path.exists():
        sys.exit(f"no brief: {brief_path} — run ./clip sort auto or ./clip new-campaign first")
    brief = json.loads(brief_path.read_text())
    required = brief.get("required_hashtags", [])
    if a.ask:
        raw = input("Required hashtags from the campaign brief (comma separated, blank if none): ")
        required = [t.strip() for t in raw.split(",") if t.strip()]
    tags = suggest_hashtags(a.campaign, required)
    print(f"hashtags ({len(tags)}/4 max): " + " ".join(tags))
    if a.ask:
        if input("save to brief? [Y/n] ").strip().lower() != "n":
            brief["required_hashtags"] = tags
            brief_path.write_text(json.dumps(brief, indent=2))
            print(f"saved to {brief_path.relative_to(ROOT)}")
            mem_log(f"hashtags for {a.campaign}: {' '.join(tags)}", "stage=hashtags")


def cmd_captions(a):
    outdir = ROOT / "out" / a.campaign
    finals = sorted(outdir.glob("*_final.mp4")) if outdir.is_dir() else []
    if not finals:
        print(f"no produced clips in out/{a.campaign}/ yet — run ./clip produce first.")
        return
    for f in finals:
        if f.with_name(f.stem + ".content.md").exists():
            print(f"  · {f.name} already has a content sheet")
            continue
        hook = a.campaign.replace("-", " ")
        for notes in sorted((ROOT / "work" / a.campaign).glob("*.notes.md")):
            body = real_transcript(notes.read_text())
            if body:
                first = re.split(r"[.!?]", body)[0].strip()
                words = first.split()
                if 3 <= len(words) <= 8:
                    hook = first.lower()
                    break
        print(f'  ./clip content "{f}" --brief briefs/{a.campaign}.json --hook "{hook}"')


def main():
    ap = argparse.ArgumentParser(description="Sort dropped videos into campaigns, assist hashtags/captions.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("auto", help="group + move uncategorized videos")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_auto)
    p = sub.add_parser("hashtags", help="suggest <=4 hashtags for a campaign")
    p.add_argument("campaign")
    p.add_argument("--ask", action="store_true", help="ask for required tags and save")
    p.set_defaults(func=cmd_hashtags)
    p = sub.add_parser("captions", help="content-sheet commands per produced clip")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_captions)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
