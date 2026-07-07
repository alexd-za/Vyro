#!/usr/bin/env python3
"""ingest.py — inventory new videos dropped in inbox/ (idempotent).

For each NEW file it probes length/resolution, writes a notes file, stages a copy under
work/_unsorted/<id>/, and records it in knowledge/inbox-manifest.json so it's never
processed twice. Assigning a video to a campaign is the agent's job (see AGENT-PLAYBOOK.md).
"""
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

INBOX = Path("inbox")
STAGE = Path("work/_unsorted")
MANIFEST = Path("knowledge/inbox-manifest.json")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height", "-of", "json", str(path)],
        stdout=subprocess.PIPE, text=True).stdout
    dur, w, h = None, None, None
    try:
        d = json.loads(out)
        dur = float(d.get("format", {}).get("duration", 0) or 0)
        for s in d.get("streams", []):
            if s.get("width"):
                w, h = s["width"], s["height"]
    except Exception:
        pass
    return dur, w, h


def transcript_snippet(path, seconds=30):
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return None
    try:
        m = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = m.transcribe(str(path))
        out = []
        for s in segs:
            if s.start > seconds:
                break
            out.append(s.text.strip())
        return " ".join(out)[:500]
    except Exception:
        return None


def load_manifest():
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text())
        except Exception:
            return {}
    return {}


def main():
    INBOX.mkdir(exist_ok=True)
    STAGE.mkdir(parents=True, exist_ok=True)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    man = load_manifest()

    files = [p for p in INBOX.iterdir()
             if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    new = 0
    for p in sorted(files):
        size = p.stat().st_size
        vid = hashlib.sha1(f"{p.name}:{size}".encode()).hexdigest()[:10]
        if vid in man:
            continue
        dur, w, h = ffprobe(p)
        snip = transcript_snippet(p)
        d = STAGE / vid
        d.mkdir(parents=True, exist_ok=True)
        dest = d / p.name
        if not dest.exists():
            shutil.copy2(p, dest)

        notes = [f"# {p.name}", "",
                 f"- id: `{vid}`",
                 f"- duration: {dur:.1f}s" if dur else "- duration: unknown",
                 f"- resolution: {w}x{h}" if w else "- resolution: unknown",
                 f"- ingested: {datetime.now().isoformat(timespec='seconds')}",
                 f"- staged at: `{dest}`", ""]
        if snip:
            notes += ["## transcript (first ~30s)", snip, ""]
        else:
            notes += ["## transcript",
                      "_(install faster-whisper for auto transcripts: pip install faster-whisper)_", ""]
        notes += ["## category",
                  "TODO — agent: assign a campaign, then move this file into work/<campaign>/", ""]
        (d / "notes.md").write_text("\n".join(notes))

        man[vid] = {"name": p.name, "size": size, "duration": dur,
                    "staged": str(dest), "category": None,
                    "ingested": datetime.now().isoformat(timespec="seconds")}
        new += 1
        print(f"  + {p.name}  ({dur:.0f}s)" if dur else f"  + {p.name}", f" -> {d}")

    MANIFEST.write_text(json.dumps(man, indent=2))
    if new == 0:
        print("no new videos in inbox/ (everything is already ingested).")
    else:
        print(f"\n{new} new video(s) staged under {STAGE}/ — categorize them into campaigns next.")
        print("agent: follow AGENT-PLAYBOOK.md → 'Categorize'.")


if __name__ == "__main__":
    main()
