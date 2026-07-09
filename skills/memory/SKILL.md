---
name: memory
description: Shared, git-native memory for EVERY AI that operates this repo (Claude Code, Codex, Gemini, any future CLI). Use at session start (recall), whenever you learn something worth keeping (add), before switching to another AI (handoff), and to search past decisions/learnings/events. One store, plain files, syncs with git.
---

# memory

One memory, every AI. The store is `knowledge/memory/events.jsonl` (append-only
JSONL, committed to git) with a human-readable view in `knowledge/MEMORY.md`.
Because it's just files + a CLI, any agent on any machine has the same brain:
clone/pull the repo and the memory arrives with it.

## The four habits (agents: make these reflexes)
```bash
./clip mem recall                 # 1. EVERY session start — loads mode, campaigns,
                                  #    top learnings/decisions, recent activity, next actions
./clip mem add "text" --type learning --tags campaign=beast-games
                                  # 2. whenever a clip over/under-performs, a rule is
                                  #    discovered, or the user states a preference
./clip mem search "captions"      # 3. before re-deciding anything — check first
./clip handoff                    # 4. before quota runs out / switching AIs:
                                  #    writes HANDOFF.md (recall + repo state)
```

## Types (pick honestly — they weight search & recall)
- `decision` — a choice that should stick ("crop reframe is default, blur only for pans")
- `learning` — evidence from results ("hooks naming MrBeast outperform generic ones")
- `note` — anything else worth keeping
- `event` — pipeline activity; **auto-logged** by `./clip` (cut/produce/ingest/publish),
  don't add these by hand

## Wiring per AI
- **Claude Code** — `.claude/settings.json` has a SessionStart hook that runs
  `./clip mem recall --hook` automatically: every new chat starts pre-briefed.
- **Codex / Gemini / others** — AGENTS.md + GEMINI.md say to run
  `./clip mem recall` first; the playbook enforces it.

## Rules
- Memory complements STATE.md, never replaces it: STATE.md = where work stands
  now (edited in place); memory = append-only history and durable knowledge.
- Never store credentials or campaign-confidential URLs in memory — it's committed.
- `./clip mem digest` regenerates MEMORY.md after bulk edits to the JSONL.
