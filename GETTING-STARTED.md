# Getting started

Clip Factory turns **approved Vyro campaign footage** into short-form clips for
TikTok / Reels / Shorts, with two AI agents (Claude Code + Codex) sharing one brain.
This is the full walkthrough. The short version lives in `README.md`.

---

## 1. Install (Fedora)

```bash
# system packages
sudo dnf install -y python3 python3-pip git

# ffmpeg needs RPM Fusion on Fedora
sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install -y ffmpeg

# optional but recommended: download approved source videos
sudo dnf install -y yt-dlp        # or: pipx install yt-dlp
```

Then, in this folder:

```bash
chmod +x clip
./clip setup     # creates folders, a python venv, .env, agent config, then runs doctor
./clip doctor    # everything required should say [ok]; fix any [!!] lines
```

`doctor` is your friend — run it any time something feels off. It checks every tool
and tells you the exact command to fix what's missing.

---

## 2. Connect the agents (optional, do once)

You already run Claude Code and Codex. Open this folder in either one and paste the
contents of `INSTALL-PROMPT.md`. It vets and installs the extra skills/tools **one at
a time** (skipping anything fake or unsafe). Because `AGENTS.md` is the shared source
of truth, both CLIs follow the same rules — when one runs out of quota, switch to the
other and keep going.

### Optional: Composio tool layer (one tool layer for both agents)
Composio connects both agents to Google Sheets/Drive, Notion, Slack, Gmail, YouTube and
more through one managed-auth endpoint — handy for logging posts to a live Vyro
dashboard, storing clips, or getting "batch ready" pings. Set it up once:

1. Get an API key at composio.dev → put `COMPOSIO_API_KEY` in `.env`.
2. Connect the apps you want in the Composio dashboard (Sheets, Drive, Notion…).
3. Add it to Claude Code: `claude mcp add --scope user --transport http composio https://connect.composio.dev/mcp`
   (and the same endpoint in Codex), then ask e.g. *"append the newest ledger row to my
   Vyro Google Sheet."*

It is **not** a TikTok poster — public posting still goes through step 5. Full notes in
`skills/composio/SKILL.md`.

---

## 3. Your first campaign

```bash
./clip new-campaign beast-games        # makes work/beast-games, out/beast-games, briefs/beast-games.json
```

1. Open `briefs/beast-games.json` and fill in the campaign's actual rules: the
   scroll-stopping caption, required hashtags, any `#ad`/AI disclosure, length limits.
2. Download the **approved** source video into `work/beast-games/`.
   (Only ever clip source the campaign provides or approves — random/AI source is
   rejected by Vyro and can get you banned.)

---

## 4. Find the moments

```bash
./clip select work/beast-games/source.mp4 --count 12 --outdir work/beast-games/candidates
```

Open `candidates.md`. It ranks the most energetic moments with timestamps and a ready
`ffmpeg` cut command for each. **You** pick the strongest 8–15 by hook quality (install
`faster-whisper` to get spoken-text per clip, which makes this much easier). Tighten
each in/out by a second or two so it starts on the hook and ends on the payoff, then cut.

---

## 5. Publish (safely)

```bash
# build the post package from a cut + the brief
./clip publish prepare --clip out/beast-games/clip_03.mp4 --brief briefs/beast-games.json

# dry-run first — prints exactly what it would post, changes nothing
./clip publish publish --package post_package.json --via scheduler

# when it looks right, actually send it
./clip publish publish --package post_package.json --via scheduler --send
```

It runs a compliance check (public? AI label? required hashtags? ≥3s?) and **refuses to
send if it fails**. On success it logs the live URL to `knowledge/ledger.md` — paste
that URL into Vyro to get paid. Add `--auto` to skip the confirm prompt only once you
trust it. Posting credentials go in `.env` (a scheduler key, or your own TikTok app
token) — until then everything stays in dry-run.

---

## 6. The daily loop

```
./clip status        # what's in flight + posts logged for Vyro
```

After each clip's results come in, jot one line in `knowledge/learnings.md` about why it
won or flopped. That file is how the system gets better over time — and it's worth more
than any blind tweak, because it's based on your real numbers.

**The one rule that protects all of this:** approved source only, disclose AI where
required, and never let unattended automation post on your behalf. A live account that
earns beats a banned one that doesn't.
