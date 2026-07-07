---
name: clip-select
description: Find the strongest candidate clips inside a long source video. Use when you have an APPROVED Vyro campaign source video and need to pick which 8-15 moments to cut into short-form clips, instead of scrubbing the whole thing by hand. Outputs ranked timestamped candidates with cut commands.
---

# clip-select

Finds candidate viral moments in a source video using audio energy (loud/emphatic
moments), silence (clean clip edges), and scene changes (visual hooks). Produces a
ranked shortlist so you cut the best parts instead of watching the whole video.

## When to use
- You've accepted a Vyro campaign and downloaded its **approved** source video.
- You need to decide which moments become clips.

## Run it
```bash
python3 find_moments.py <source.mp4> --count 12 --target 22 --outdir ./candidates
```
Useful flags: `--min` / `--max` clip length (s), `--target` ideal length,
`--count` how many to return, `--transcribe` (attaches spoken text per candidate
if `faster-whisper` is installed — install with `pip install faster-whisper`).

Outputs `candidates.json` and `candidates.md` (ranked, with per-clip cut commands).

## How to use the output (this is the important part)
The script finds *energetic* moments — it does NOT understand meaning. After it runs:
1. Read `candidates.md`. If transcripts are attached, judge each by **hook quality**:
   does the first line stop a scroll? Is there a payoff, reveal, or emotional spike?
2. Cross-check the **campaign brief**: required length, what the campaign wants
   highlighted, banned content, mandatory hashtags/disclosures.
3. Pick the strongest **8-15** — not all of them. Quality over volume per clip;
   volume comes from doing this across many source videos.
4. For each pick, refine the in/out by a second or two so it starts on the hook and
   ends on the punch. Then cut (the `ffmpeg -ss ... -to ...` command is in the file).
5. Hand the cuts to the reframe/caption steps, then `quality-gate`, then `publish`.

## Notes
- Runs ffmpeg over the file a few times; fine for a CLI step, slower on very long videos.
- No audio? It falls back to scene-change + even sampling, lower confidence — eyeball those.
- Only ever run this on source you're licensed to clip for the campaign.
