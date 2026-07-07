@AGENTS.md

## Claude Code — specific
- Use **plan mode** for anything that touches the publishing/scheduling step or account credentials.
- Use **subagents** to parallelize a batch: one selects clips, one cuts/reframes, one captions — then review the results together.
- Prefer the local recipes in `./skills` over re-deriving a workflow each time.
- Never run a step that posts to a live social account without showing me the full batch first.
