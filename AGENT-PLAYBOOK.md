# Operator Playbook — read this first, every session

You are operating the **Clip Factory**. Your job: turn the videos the user drops in
`inbox/` into ready-to-post clips with content sheets and covers — and never lose your
place across sessions. The user may close the app, start a new chat, or switch to a
different AI; continuity lives in files on disk, not in this conversation.

## On every start (do this in order)
0. **Load memory:** `./clip mem recall` — mode, campaigns, top learnings/decisions,
   recent activity, next actions. (Claude Code runs this automatically via the
   SessionStart hook in `.claude/settings.json`; other AIs run it by hand.)
1. **Read `STATE.md`.** It is the source of truth for where things are. If it conflicts
   with what you think you remember, STATE.md wins.
2. **Check the mode:** run `./clip mode` (or read `MODE` in `.env`).
   - `offline` → make clips, content sheets, and covers only. **Never post.**
   - `online` → also publish via the configured backend.
3. **Ingest + sort the inbox:** run `./clip ingest`, then `./clip sort auto` (propose,
   confirm, move). It names campaigns from filenames/transcripts, files each video under
   `work/<campaign>/`, writes CAMPAIGN-NOTES.md, and creates the brief. Then
   `./clip hashtags <campaign> --ask` records required hashtags and suggests up to 4 total.
   (The web UI — `./clip ui` — does ingest+sort automatically on every drop.)
4. Read the **Next actions** in STATE.md and continue from there. Tell the user, in one
   line, what you're about to do — then do it.

## The loop, per video
1. **Categorize.** From the notes ingest wrote (`work/_unsorted/<id>/notes.md` — filename,
   length, transcript snippet), decide which campaign the video belongs to. If it's
   unclear, ask the user once; otherwise file it under a campaign named `unsorted`.
   - Create the campaign if needed: `./clip new-campaign <name>` (this also makes a brief).
   - Move the video into `work/<campaign>/`.
   - Update the `category` field in `knowledge/inbox-manifest.json` and note it in STATE.md.
2. **Find moments:** `./clip select work/<campaign>/<file> --transcribe --outdir work/<campaign>/candidates`
   Read `candidates.md`.
3. **Pick + cut.** Choose the strongest 8–15 by *hook quality* (use the transcript — does
   the first line stop a scroll? is there a payoff?). Cut each: `./clip cut work/<campaign>/<file> <in> <out> out/<campaign>/clip_NN.mp4`
4. **Produce.** Finish each cut — 9:16 reframe, word-synced captions, grade, loudness:
   `./clip produce out/<campaign>/clip_NN.mp4 --brief briefs/<campaign>.json --hook "TITLE"`
   Use `--reframe blur` when the crop would lose something important at the edges;
   `--grade moody` for a calmer look; `--no-captions` for music-only footage (never
   burn song lyrics as captions).
5. **Verify the hook against the footage:** `./clip sheet out/<campaign>/clip_NN_final.mp4`
   and look at every panel. **If the video doesn't clearly show what the hook/caption
   claims, change the hook or drop the clip** — a mislabeled clip breaks the campaign's
   "don't misrepresent" rules and reads as clickbait. Also confirm length satisfies the
   brief's `min_seconds`/`max_seconds`.
6. **Write a content sheet** per clip. Draft it in one command, then polish:
   `./clip content out/<campaign>/clip_NN_final.mp4 --brief briefs/<campaign>.json --hook "<the verified hook>"`
   — that prefills title options, caption options, a pinned author comment, and the
   required hashtags (all checked against banned_phrases). Then review and fill in:
   - **Hook** — the on-screen line for the first 1–2 seconds.
   - **3 caption options** — written in the campaign's voice.
   - **Hashtags** — pull the required ones from the brief.
   - **Sound/music** — suggest a vibe (you can't attach a trending sound via API; tell the
     user to add it in the TikTok app before exporting).
   - **Cover** — a one-line concept, then generate it:
     `./clip cover out/<campaign>/clip_NN.mp4 "HOOK TEXT"` → writes `clip_NN_cover.png`.
     (Codex can run or restyle `tools/make_cover.py` if you want a different look.)
   - **Leave the two "your captions" slots blank** — those are for the user.
7. **Publish — only if mode is `online`:**
   `./clip publish prepare --clip out/<campaign>/clip_NN.mp4 --brief briefs/<campaign>.json`
   then `./clip publish publish --package post_package.json --via <backend> --send`.
   If mode is `offline`, stop here and tell the user where the clips, content sheets, and
   covers are.
8. **Update `STATE.md`:** mark this video's stage, list what's posted vs pending, and write
   the single next action. Set the "Last updated / by" line. Do this after every meaningful
   step — it's what lets the next session resume cleanly.

## Standing rules
- **Approved source only.** Disclose AI/branded content where the brief requires it.
- **Never post in offline mode**, and never post without the user having a chance to review
  — keep the publish step's dry-run/confirm behavior.
- Trending sounds can't be set by API; they go in the content sheet for the user to add in-app.
- After a clip clearly over- or under-performs, write one line in `knowledge/learnings.md`
  about why. That file is how your picks get better over time.
- Keep your messages short and tell the user what you did and what's next.
- **Feed the memory:** `./clip mem add "..." --type learning|decision` whenever a rule
  is discovered or a clip's result teaches something. Pipeline events are auto-logged.
- **Before quota runs out or you stop:** run `./clip handoff` — it writes HANDOFF.md
  (memory recall + repo state) so the next AI resumes in one read.

## Quick command reference
```
./clip ingest                      inventory new inbox videos
./clip sort auto                   auto-name campaigns + file videos into them
./clip hashtags <camp> --ask       record + suggest hashtags (4 max)
./clip ui                          web dashboard (drag & drop, produce, captions)
./clip mode | offline | online     show/switch mode
./clip select <video> --transcribe rank clip moments
./clip cut <video> <in> <out> <o>  cut one clip
./clip produce <cut> --brief <b>   finish it: 9:16 + captions + grade
./clip sheet <video>               contact sheet — hook vs footage QA
./clip cover <clip> "HOOK"         generate a cover image
./clip content <clip> --brief <b> --hook "..."   draft title/captions/pinned comment
./clip fx fonts                    fetch display fonts (hooks auto-upgrade)
./clip publish prepare|publish     build/publish a post (online mode)
./clip mem recall|add|search       shared AI memory (see skills/memory)
./clip handoff                     write HANDOFF.md for the next AI
./clip state                       show STATE.md
./clip dashboard                   campaigns + posts logged
```
