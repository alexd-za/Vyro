---
name: composio
description: Connect BOTH agents (Claude Code + Codex) to external apps — Google Sheets/Drive, Notion, Slack, Gmail, YouTube and 500+ more — through ONE managed-auth layer (Composio). Use to log posts to a live Vyro dashboard, archive clips/source, send "batch ready" notifications, and pull research. NOT a TikTok auto-poster; public TikTok posting still goes through the publish skill.
---

# composio

Composio is a managed auth + tool gateway: one endpoint that lets an agent act across
500+ apps, with OAuth and token refresh handled for you. Here it's the **shared tool
layer both Claude Code and Codex call** — connect an app once, either agent can use it.

## What it's for in this project
- **Live Vyro dashboard** — push each shipped clip + its URL/views from
  `knowledge/ledger.md` into a Google Sheet or Notion DB you can actually track.
- **Storage** — archive approved source and finished clips to Google Drive.
- **Notifications** — Slack/Gmail "batch ready to review" or "clip crossed 5k views".
- **Research** — pull trends/comments from connected apps to inform the next batch.

## Posting to TikTok through Composio
Composio DOES have a TikTok toolkit (`TIKTOK_PUBLISH_VIDEO`, `TIKTOK_UPLOAD_VIDEO`,
`TIKTOK_POST_PHOTO`, status + stats tools). `TIKTOK_PUBLISH_VIDEO` pulls the video from
a public URL and posts via TikTok's **official Content Posting API**. You connect your
OWN TikTok credentials in the Composio dashboard; Composio just manages the OAuth
lifecycle. Use it from the pipeline:
```bash
./clip publish publish --package post_package.json --via composio --send
```
(needs `COMPOSIO_API_KEY`, a connected TikTok account, the composio SDK
`pip install composio`, and a public `video_url` in the package.)

**Still the same rules underneath:** because it's the official API, your TikTok app must
clear TikTok's audit for public (non-private) posts, AI/branded content must be disclosed,
and you post to YOUR OWN account. It's a clean managed-auth route, not a way around the
audit. The publish skill's compliance gate + dry-run default still apply.

## Setup (uses YOUR own account + key)
1. Create a Composio account and an API key at composio.dev (dashboard → API settings).
2. Put it in `.env`:  `COMPOSIO_API_KEY=...`
3. In the Composio dashboard, **connect the apps** you want (Google Sheets, Drive,
   Notion, Slack, Gmail, YouTube…). Connecting on the website first is smoother.
4. Make both agents share it (Tool Router gives a single MCP endpoint):
   - **Claude Code:** `claude mcp add --scope user --transport http composio https://connect.composio.dev/mcp`
     then run `/mcp` to confirm `composio` is connected.
   - **Codex:** add the same Composio MCP endpoint via Codex's MCP config / Composio CLI.
   - Or copy `.mcp.json.example` → `.mcp.json` for a project-scoped setup.
   - Endpoints/auth headers change — confirm the current one at docs.composio.dev.

## How the agents use it
Just ask in plain language; the Tool Router finds the right tool and prompts an auth
link if the app isn't connected yet. Examples:
- "Append the newest row in knowledge/ledger.md to my 'Vyro' Google Sheet."
- "Upload out/beast-games/clip_03.mp4 to the Drive folder 'clips/beast-games'."
- "Slack me a summary of today's shipped clips."

## Notes
- Free tier is generous (~20K calls/month). Scope permissions tightly in the dashboard;
  there's a full audit log of what the agent did, on whose behalf.
- Keep `COMPOSIO_API_KEY` in `.env` only (git-ignored). Never paste it into a clip caption
  or commit it.
