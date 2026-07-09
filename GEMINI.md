# Clip Factory — Gemini CLI entrypoint

Read **AGENTS.md** first — it is the single source of truth for this project
(same instructions Claude Code and Codex use). Then:

1. Run `./clip mem recall` to load the shared memory (state, learnings, next actions).
2. Follow **AGENT-PLAYBOOK.md** for the operating loop.
3. Before your session ends, `./clip mem add` anything worth keeping and run
   `./clip handoff` so the next AI resumes instantly.
