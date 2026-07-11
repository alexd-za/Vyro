# Memory digest

_Human-readable view of `knowledge/memory/events.jsonl`. Regenerate with_
_`./clip mem digest`. Any AI: run `./clip mem recall` at session start._

## Decisions
- 2026-07-11 [decision] Music-only footage gets a vibe edit (tag + end card), never burned song-lyric captions. Talking-head footage gets word-synced captions with brand-keyword highlights. (stage=produce)
- 2026-07-11 [decision] Vyro campaigns commonly require: 15s minimum length, no health/efficacy claims in captions, likes/comments on, no boosting/paid promotion, no unaffiliated logos or content. Encode these in the brief (min_seconds, banned_phrases) before producing. (source=motrin-campaign)

## Learnings
- 2026-07-11 [learning] Two source clips (~87s) yield at most 4 genuinely distinct verticals; a 5th becomes a near-duplicate reviewers will flag. More output requires more approved source footage. (source=motrin-campaign)
- 2026-07-11 [learning] Never ship a hook the footage does not clearly show — a 'locker spotlight' cut was rejected because only ~2s of lockers existed. Always run ./clip sheet and check every panel. (source=motrin-campaign)
