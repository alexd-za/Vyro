# Clip Factory — starter kit

A foundation for running **Claude Code + Codex as one shared system** on your Fedora box, pointed at a
**Vyro clipping pipeline** (turn approved campaign footage into TikTok/Reels/Shorts clips that earn views).

## What's in here
- `AGENTS.md` — the canonical instructions. **Both** Codex (reads it automatically) and Claude Code use this.
- `CLAUDE.md` — a one-line wrapper that imports `AGENTS.md` plus a few Claude-only notes.
- `INSTALL-PROMPT.md` — paste this into Claude Code or Codex and it installs the **vetted** toolchain itself.
- `setup-agents.sh` — safe, local-only scaffold (folders + knowledge base + the import wiring). Downloads nothing.

## Start here
```bash
chmod +x clip
./clip setup     # folders, python venv, .env, agent config, then a health check
./clip           # opens the menu — or use ./clip doctor, new-campaign, select, publish
```
Then (optional) open this folder in Claude Code or Codex and paste `INSTALL-PROMPT.md`
to pull in the extra skills/tools. **Full walkthrough: `GETTING-STARTED.md`.**

## How the dual-agent setup works
- One source of truth (`AGENTS.md`), so the two CLIs never drift.
- Portable `SKILL.md` skills live in `./skills` and work in both tools (in Codex you call them with `/skill-name`).
- A markdown knowledge base (`./knowledge`) is the memory: `ledger.md` tracks shipped clips + views, `learnings.md`
  records why clips won or lost. That's the "self-improving" loop — it compounds because every result is written down.
- Run out of quota on one tool → switch to the other. Same brain, no re-explaining.

## Why a few things were left out on purpose
- **No pasted "Fable 5"/extracted system prompt.** It can't reprogram the model and only bloats context. A lean,
  task-specific instruction file (this one) is what actually improves output.
- **No blind bulk-install of every URL.** The install prompt vets each source and skips anything fake, abandoned,
  or built for bot-detection evasion / prompt extraction — that's malware-shaped risk on a machine you actually use.
- **No fully-autonomous posting.** It violates TikTok's terms and gets accounts banned, which zeroes your Vyro
  earnings. The pipeline prepares + schedules; you confirm each batch. (Clip *volume and quality* is the real lever.)

## The Vyro rule that shapes everything
You may only clip **source content the campaign owner provides or approves**. Random or AI-generated source gets
rejected. So this whole system is built to make many strong clips from *approved* footage — fast.
