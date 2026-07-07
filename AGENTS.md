# Clip Factory — Agent Instructions

> **Operating this autonomously?** Read **`AGENT-PLAYBOOK.md`** first — it's the
> step-by-step loop. Track everything in **`STATE.md`** (it survives across sessions and
> different AIs). Videos go in **`inbox/`**; run `./clip ingest` to pick them up.

## What this project is
A short-form video **clipping** pipeline for the Vyro platform (vyro.com).
We turn **approved campaign source videos** into vertical clips for TikTok, Reels, and YouTube Shorts.
Goal: many high-quality clips, fast, that earn views. Vyro pays roughly $3 per 1,000 views.

## Hard rules (read these first)
- Only ever clip source video from an **active Vyro campaign we have accepted**. Never clip random or third-party videos — Vyro rejects those and it can get the account banned.
- Follow each campaign brief exactly: required hashtags, disclosures (e.g. #ad), caption rules, min/max length, allowed edits.
- Disclose AI-assisted or AI-generated elements wherever the platform or the campaign requires it.
- **Never post fully autonomously** to a social account. Prepare and schedule; a human reviews and confirms each batch before it goes live. Unattended bot posting gets accounts banned, which kills the income.
- Keep everything reproducible: every clip = inputs + a command we can re-run.

## Both agents read this file
This is the single source of truth for **both Claude Code and Codex**.
- Codex reads `AGENTS.md` automatically.
- Claude Code reads `CLAUDE.md`, which imports this file (`@AGENTS.md`) and adds Claude-only notes below the import.
When one runs out of quota, switch to the other — same instructions, same skills, same memory. Do not re-explain the project.

## The pipeline (stages)
1. **Research** — find what's hooking on the campaign's topic right now. (skill: `trend-research`)
2. **Select** — pick the 8–15 strongest moments from a source video. (skill: `clip-select`)
3. **Cut** — trim to the moment. (`./clip cut`)
4. **Produce** — vertical 9:16 reframe, punch-in, color grade, word-synced animated
   captions with brand-keyword highlights, hook title, loudness. (`./clip produce`)
5. **Polish** — optional motion graphics / overlays. (Lottie / GSAP / motion skills)
6. **QA** — `./clip sheet` contact sheet: confirm the footage shows what the hook
   claims (never ship a mislabeled clip), and length fits the brief.
7. **Package** — name, first-frame, per-platform export, caption text + hashtags pulled from the brief.
8. **Schedule** — queue to a human-reviewed posting tool; log the live URL back for Vyro submission.

## Conventions
- Work in `./work/<campaign>/<source-id>/` ; final exports in `./out/<campaign>/`.
- One clip = one folder containing: source ref, in/out timestamps, the command used, `caption.txt`, `export.mp4`.
- Log every shipped clip + its live URL in `knowledge/ledger.md` so we can submit to Vyro and track views.
- When a clip over- or under-performs, write one line in `knowledge/learnings.md` about why. This is how the system improves over time.

## Progressive disclosure (load on demand, not every session)
- `skills/` — reusable recipes, one `SKILL.md` each. In Codex, invoke with `/skill-name`.
- `knowledge/INDEX.md` — campaign notes, hooks that landed, what's worked.
- `TOOLS.md` — installed tools and what each is for.
- `INSTALL-PROMPT.md` — how to (re)install the toolchain safely.
- `skills/composio/` — optional shared tool layer (Sheets/Drive/Notion/Slack) both
  agents call via one Composio MCP endpoint. Use it to log the ledger to a live Vyro
  dashboard, store clips, or send notifications — NOT to post to TikTok (that's `publish`).

## When stuck
Different model, different idea. If one agent is spinning on a problem, hand it to the other with the plan written to `PLAN.md`.
