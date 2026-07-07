---
name: publish
description: Prepare and publish a finished clip to TikTok/Reels/Shorts the LEGITIMATE way — via TikTok's official Content Posting API or a pre-audited scheduler — with a compliance check, optional human confirm, and automatic logging of the live URL for Vyro. Use when a clip has passed quality-gate and is ready to go out. Does NOT do browser/bot automation.
---

# publish

Turns a finished clip into a live post **without the part that gets accounts banned**.

## The two legitimate roads (read first)
TikTok has exactly one officially supported way to post programmatically: the
**Content Posting API** (OAuth, `open.tiktokapis.com`). Two important catches:

1. **Audit gate.** Until *your own* app passes TikTok's audit (~2–6 weeks, needs a
   documented use case), every post is forced **private (SELF_ONLY)** and only 5
   accounts/day can use it. Private posts earn zero Vyro views — so for solo use the
   audit is the blocker.
2. **The shortcut:** route through a **pre-audited unified scheduler** (e.g. Post for
   Me, PostPeer, Zernio, Postproxy). You connect your TikTok once via OAuth; they own
   the audited access, so you can publish **public** posts immediately and cross-post
   to Reels + Shorts in one call (Vyro counts all three). Cheap — roughly $10 per
   1,000 posts at the low end.

**What this skill will NOT do:** drive the TikTok app/web with Selenium/Playwright,
use anti-detect browsers, or run multiple/throwaway accounts. That's platform
manipulation, it's against TikTok's rules, and a banned account = $0 from Vyro.

## Why keep a thin human gate even with "autopost"
Vyro is structurally human-in-the-loop: each campaign brief dictates exact
caption/hashtags/**disclosures** (incl. the AI-content label), and you must paste
**your live post URL** back into Vyro to get paid. So the smart design isn't "fully
hands-off" — it's "fully prepared + scheduled, one-tap confirm, auto-logged."

## Flow
```bash
# 1. Build the post package from a clip + the campaign brief
python3 publish.py prepare --clip out/<campaign>/clip_03.mp4 --brief briefs/<campaign>.json

# 2. Publish it (DRY-RUN by default — prints exactly what it would send)
python3 publish.py publish --package post_package.json --via scheduler   # or: --via official | --via composio
#    add --send to actually publish, --auto to skip the confirm prompt
```
- `prepare` writes `post_package.json`: video path, caption, hashtags, AI-content
  flag, privacy=public, target platforms — all pulled from the brief.
- `publish` runs a **compliance check** (AI label set? brief hashtags present?
  privacy public? duration ≥ 3s?) and refuses to send if it fails.
- On success it appends the returned live URL to `../../knowledge/ledger.md` so it's
  ready to submit to Vyro and track views.

## Setup (where your credentials plug in)
- Official API: set `TIKTOK_ACCESS_TOKEN` (scope `video.publish`) + `TIKTOK_CLIENT_KEY`.
- Scheduler: set `SCHEDULER_BASE_URL` + `SCHEDULER_API_KEY` for your chosen provider.
- Field names on TikTok's API change — if a request 400s, check the current docs at
  developers.tiktok.com/doc/content-posting-api and update `publish.py` accordingly.

## Default posture
Ship in **review-queue mode** (`prepare` + scheduled, you confirm batches) until your
account/app is trusted and the compliance check is reliably green. Flip to `--auto`
only once you've watched it behave and disclosures are handled correctly.
