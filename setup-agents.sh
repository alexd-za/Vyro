#!/usr/bin/env bash
set -euo pipefail

# Safe, local-only scaffolding. This script does NOT download or run any remote code.
# It just creates the folder structure and wires the dual-agent config.

mkdir -p skills knowledge work out

if [ ! -f AGENTS.md ]; then
  echo "WARNING: AGENTS.md is missing — drop it in the repo root before running the agents."
fi

# CLAUDE.md = thin import wrapper so Claude Code and Codex share one source of truth.
if [ ! -f CLAUDE.md ]; then
  printf '@AGENTS.md\n\n## Claude Code — specific\n- Never post to a live account without showing me the batch first.\n' > CLAUDE.md
  echo "Created CLAUDE.md (imports AGENTS.md)."
fi

# Seed the knowledge base used for memory + self-improvement.
[ -f knowledge/INDEX.md ] || printf '# Knowledge index\n\n- learnings.md — why clips won or lost\n- ledger.md — every shipped clip + its live URL + views\n- Add campaign-specific notes as their own files here.\n' > knowledge/INDEX.md
[ -f knowledge/learnings.md ] || printf '# Learnings\n\nOne line per clip that over- or under-performed, and why.\n' > knowledge/learnings.md
[ -f knowledge/ledger.md ] || printf '# Ledger\n\n| date | campaign | clip | platform | url | views |\n|------|----------|------|----------|-----|-------|\n' > knowledge/ledger.md

echo ""
echo "Scaffold ready."
echo "Next: open Claude Code or Codex in this folder and paste the contents of INSTALL-PROMPT.md"
