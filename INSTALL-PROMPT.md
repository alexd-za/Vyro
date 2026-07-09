# INSTALL PROMPT — paste into Claude Code or Codex, running inside this project folder

You are setting up the toolchain for a **Vyro clipping pipeline** on a Fedora Linux machine.
Work carefully and **safely**. The user is watching. Do not run anything you can't explain in one line.

> **The basics are one command now:** `./install.sh` covers ffmpeg, python, the venv, and the
> caption/cover extras. This prompt is for the OPTIONAL layer beyond that — vetted third-party
> skills, publishing-tool selection, and Codex wiring. Skip any step install.sh already did.

## Ground rules (non-negotiable)
- Before installing anything from a URL, fetch its README and tell me, in one line: what it is, whether the repo actually exists, and whether it looks maintained. If you can't verify it, **SKIP it and say why**. Never pipe `curl` into `bash` from an unverified source.
- Install **one tool at a time**. After each, run its `--version`/`--help` to confirm it works. Stop and ask if anything fails or wants root beyond a normal package install.
- **Do NOT install anything whose purpose is** bypassing platform bot-detection, evading moderation, scraping behind logins you don't own, or "jailbreaking"/extracting model prompts. If a listed item is that, skip it and say so out loud. (This explicitly includes anti-detect browsers and prompt-extraction repos.)
- Maintain a running `TOOLS.md`: tool | source | one-line purpose | install command | verified Y/N.

## Core toolchain (install these first — the workhorses)
1. **ffmpeg** — the actual cutting / reframing / caption-burn engine. (On Fedora this usually needs RPM Fusion enabled first — set that up, then install.)
2. **yt-dlp** — used **only** to download approved Vyro campaign source we're licensed to clip. (via `pipx`)
3. **Python 3** + a venv at `./.venv`. Then `pip install`: `scrapling` (trend research), `pillow`, plus whatever a skill needs.
4. **OpenCut** (opencut.app) — open-source editor for manual finishing. Verify the repo first, then follow its README.
5. A **scheduling / publishing** tool for human-reviewed posting. Recommend 2–3 options that use official platform APIs. Install **none** that need posting credentials until I confirm which one.

## Skills to pull in (vet each; copy its `SKILL.md` into `./skills/<name>/`)
Check each repo exists and is real, then add the useful ones:
- `mattpocock/skills`, `addyosmani/agent-skills`, `emilkowalski/emil-design-eng`, `ui-ux-pro-max-skill` — design / web (keep for later projects)
- `hardikpandya/stop-slop` — quality gate; wire it into our QA stage
- `greensock/gsap-skills`, `199-biotechnologies/motion-dev-animations-skill` — motion graphics for captions/overlays
- Any genuine video/montage skill relevant to clipping (e.g. OpenMontage, claude-video) — **verify before adding**
For anything you can't find, or that looks abandoned/unsafe: skip it and log the reason in `TOOLS.md`.

## Memory / self-improvement
- Confirm the local knowledge base exists: `./knowledge/INDEX.md`, `learnings.md`, `ledger.md` (already referenced by AGENTS.md).
- If a memory MCP is available and you can verify it's trustworthy, wire it in. Otherwise the markdown knowledge base is the source of truth.

## Dual-agent wiring (so Codex and Claude Code share one brain)
- Confirm `AGENTS.md` (canonical) and `CLAUDE.md` (which imports `@AGENTS.md`) both exist at the repo root.
- In `~/.codex/config.toml`, add: `project_doc_fallback_filenames = ["CLAUDE.md"]`
- Put portable skills in `./skills` (both tools read them); Codex can also use `~/.codex/skills/` for globals.

## When done
Print `TOOLS.md` and a 5-line "what works / what's still pending" summary, plus the exact commands to re-run the install.
