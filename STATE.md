# STATE  —  the resumable source of truth

This file is how any AI (a new chat, a reopened app, or a different model) picks up
exactly where the last one stopped. **Whoever operates this project reads this first and
keeps it current.** If it ever conflicts with what you think you remember, this file wins.

_Last updated: (set when you edit) · by: (which AI/session)_

---

## Mode
**offline**
<!-- offline = make clips + content sheets only, never post.
     online  = also post via the configured backend.
     The real switch is ./clip mode — keep this line matching it. -->

## Campaigns
<!-- one per active campaign, e.g.  - beast-games  (brief: briefs/beast-games.json) -->
(none yet)

## Inbox / unsorted
<!-- videos ingested but not yet assigned to a campaign. Run ./clip ingest to refresh. -->
(run ./clip ingest)

## In progress
<!-- per clip: what stage it's at — selected / cut / content-sheet / cover / posted -->
(nothing yet)

## Posted
<!-- mirror of knowledge/ledger.md; the live URLs you've submitted to Vyro -->
(none)

## Next actions
- Drop videos in `inbox/`, run `./clip ingest`, then categorize them into campaigns.
