#!/usr/bin/env python3
"""memory.py — one memory, every AI.

A git-native memory store any agent CLI (Claude Code, Codex, Gemini, ...) can
read and write through `./clip mem ...`. Plain files, no server, no deps:
syncs across machines with the repo, diffs in PRs, survives any chat ending.

  add "text" [--type note|learning|decision] [--tags k=v,...]   remember something
  log "text" [--tags ...]     append a pipeline event (quiet; used by ./clip)
  search "query" [--limit N]  keyword search, recency- and type-weighted
  recall [--hook]             compact context pack for session start
  digest                      rewrite knowledge/MEMORY.md (human-readable view)

Store: knowledge/memory/events.jsonl — append-only, one JSON object per line:
  {"ts": iso8601, "type": "note|learning|decision|event", "text": ..., "tags": {...}}
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "knowledge" / "memory" / "events.jsonl"
DIGEST = ROOT / "knowledge" / "MEMORY.md"
STATE = ROOT / "STATE.md"
TYPE_W = {"decision": 1.5, "learning": 1.3, "note": 1.0, "event": 0.6}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load():
    if not STORE.exists():
        return []
    out = []
    for line in STORE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a corrupt line never takes the store down
    return out


def append(entry):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with STORE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_tags(s):
    tags = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k.strip()] = v.strip()
        elif part.strip():
            tags[part.strip()] = "true"
    return tags


def age_days(ts):
    try:
        then = datetime.fromisoformat(ts)
        if then.tzinfo is None:
            then = then.astimezone()
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 365.0


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def score(e, qtoks):
    toks = tokens(e.get("text", ""))
    for v in e.get("tags", {}).values():
        toks |= tokens(str(v))
    if qtoks:
        hit = len(qtoks & toks)
        if hit == 0:
            return 0.0
        base = hit / len(qtoks)
    else:
        base = 1.0
    # recency half-life ~1 month; decisions/learnings age slower than events
    return base * TYPE_W.get(e.get("type"), 1.0) * math.exp(-age_days(e.get("ts", "")) / 45.0)


def fmt(e, with_ts=True):
    ts = (e.get("ts") or "")[:10]
    tag = " ".join(f"{k}={v}" for k, v in e.get("tags", {}).items())
    head = f"[{e.get('type','note')}]"
    parts = [f"- {ts}" if with_ts else "-", head, e.get("text", "")]
    if tag:
        parts.append(f"({tag})")
    return " ".join(p for p in parts if p)


def state_next_actions():
    if not STATE.exists():
        return []
    lines, active = [], False
    for line in STATE.read_text().splitlines():
        if line.startswith("## "):
            active = line.lower().startswith("## next")
            continue
        if active and line.strip().startswith("-"):
            lines.append(line.strip())
    return lines[:5]


def campaigns():
    work = ROOT / "work"
    if not work.is_dir():
        return []
    return sorted(d.name for d in work.iterdir() if d.is_dir() and not d.name.startswith("_"))


def mode():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("MODE="):
                return line.split("=", 1)[1].strip() or "offline"
    return "offline"


def cmd_add(a):
    append({"ts": now_iso(), "type": a.type, "text": a.text, "tags": parse_tags(a.tags)})
    print(f"remembered ({a.type}).")
    cmd_digest(a, quiet=True)


def cmd_log(a):
    append({"ts": now_iso(), "type": "event", "text": a.text, "tags": parse_tags(a.tags)})


def cmd_search(a):
    qtoks = tokens(a.query)
    ranked = sorted(((score(e, qtoks), e) for e in load()), key=lambda x: -x[0])
    hits = [(s, e) for s, e in ranked if s > 0][: a.limit]
    if not hits:
        print("no matches.")
        return
    for s, e in hits:
        print(fmt(e))


def cmd_recall(a):
    entries = load()
    events = [e for e in entries if e.get("type") == "event"][-5:]
    keepers = sorted((e for e in entries if e.get("type") != "event"),
                     key=lambda e: -score(e, set()))[:8]
    out = []
    if a.hook:
        out.append("Clip Factory memory (auto-recall — run `./clip mem search <q>` for more):")
    else:
        out.append("# Clip Factory — memory recall")
    camps = ", ".join(campaigns()) or "none yet"
    out.append(f"mode: {mode()} · campaigns: {camps}")
    if keepers:
        out.append("## worth knowing (learnings / decisions / notes)")
        out += [fmt(e) for e in keepers]
    if events:
        out.append("## recent activity")
        out += [fmt(e) for e in events]
    nxt = state_next_actions()
    if nxt:
        out.append("## next actions (STATE.md)")
        out += nxt
    if len(entries) == 0:
        out.append("(memory is empty — add with: ./clip mem add \"...\" --type learning)")
    print("\n".join(out[:40] if a.hook else out))


def cmd_digest(a, quiet=False):
    entries = load()
    groups = {"decision": [], "learning": [], "note": [], "event": []}
    for e in entries:
        groups.setdefault(e.get("type", "note"), []).append(e)
    out = ["# Memory digest",
           "",
           "_Human-readable view of `knowledge/memory/events.jsonl`. Regenerate with_",
           "_`./clip mem digest`. Any AI: run `./clip mem recall` at session start._",
           ""]
    for kind, title in [("decision", "Decisions"), ("learning", "Learnings"),
                        ("note", "Notes"), ("event", "Recent events (last 15)")]:
        items = groups.get(kind, [])
        if kind == "event":
            items = items[-15:]
        if not items:
            continue
        out.append(f"## {title}")
        out += [fmt(e) for e in reversed(items)]
        out.append("")
    DIGEST.write_text("\n".join(out).rstrip() + "\n")
    if not quiet:
        print(f"wrote {DIGEST.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description="Shared memory for every AI operating this repo.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="remember something")
    p.add_argument("text")
    p.add_argument("--type", choices=["note", "learning", "decision"], default="note")
    p.add_argument("--tags", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("log", help="append a pipeline event (quiet)")
    p.add_argument("text")
    p.add_argument("--tags", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("search", help="keyword search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recall", help="compact context pack for session start")
    p.add_argument("--hook", action="store_true", help="short form for agent hooks")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("digest", help="rewrite knowledge/MEMORY.md")
    p.set_defaults(func=cmd_digest)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
