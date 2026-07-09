#!/usr/bin/env bash
# smoke.sh — end-to-end pipeline check on synthetic footage. No network, no creds.
#   bash tests/smoke.sh          (run from the repo root)
# Copies the toolkit into a temp sandbox so the repo stays clean.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
cp -r clip tools skills content-template.md .env.example "$SANDBOX/"
cd "$SANDBOX"
mkdir -p inbox knowledge briefs out work
PASS=0; FAIL=0
check(){ if eval "$2"; then PASS=$((PASS+1)); echo "  ok: $1"; else FAIL=$((FAIL+1)); echo "  FAIL: $1"; fi }

echo "== synthesize test footage =="
ffmpeg -y -v error -f lavfi -i testsrc2=size=1280x720:rate=30:duration=20 \
  -f lavfi -i "sine=frequency=440:duration=20" \
  -c:v libx264 -preset veryfast -c:a aac -shortest inbox/synth.mp4

echo "== ingest =="
python3 tools/ingest.py >/dev/null
check "manifest written"            '[ -f knowledge/inbox-manifest.json ]'
check "video staged with notes"     'ls work/_unsorted/*/notes.md >/dev/null 2>&1'
python3 tools/ingest.py | grep -q "no new videos" && echo "  ok: ingest is idempotent" && PASS=$((PASS+1)) || { echo "  FAIL: ingest re-processed"; FAIL=$((FAIL+1)); }

echo "== select =="
python3 skills/clip-select/find_moments.py inbox/synth.mp4 --count 4 --outdir cand >/dev/null 2>&1
check "candidates found"            'python3 -c "import json,sys; sys.exit(0 if json.load(open(\"cand/candidates.json\")) else 1)"'

echo "== cut =="
./clip cut inbox/synth.mp4 2 18 out/cut.mp4 >/dev/null
check "cut written"                 '[ -f out/cut.mp4 ]'

echo "== produce (no-whisper fallback + video-only input) =="
python3 tools/produce.py out/cut.mp4 --hook 'SMOKE\nTEST' --out out/final.mp4 2>/dev/null
check "finished clip is 1080x1920"  'ffprobe -v error -show_entries stream=width,height -of csv=p=0 out/final.mp4 | grep -q 1080,1920'
ffmpeg -y -v error -f lavfi -i testsrc2=size=640x360:rate=30:duration=5 -c:v libx264 -preset veryfast vonly.mp4
python3 tools/produce.py vonly.mp4 --no-captions --reframe blur --grade none --out out/vonly.mp4 2>/dev/null
check "video-only input renders"    '[ -f out/vonly.mp4 ]'

echo "== sheet =="
./clip sheet out/final.mp4 out/sheet.png >/dev/null
check "contact sheet written"       '[ -f out/sheet.png ]'

echo "== publish gate v2 =="
cat > briefs/t.json <<'EOF'
{"campaign":"t","caption_template":"nice clip works fast","required_hashtags":["#x"],
 "banned_phrases":["works fast"],"min_seconds":30,"ai_generated":true}
EOF
python3 skills/publish/publish.py prepare --clip out/final.mp4 --brief briefs/t.json --out pkg.json >/dev/null
check "gate rejects banned phrase + short clip + missing AI label" \
  '! python3 skills/publish/publish.py publish --package pkg.json >/dev/null 2>&1'
python3 - <<'PY'
import json
p = json.load(open("pkg.json"))
p.update(caption="nice AI clip #x", min_seconds=5, banned_phrases=[])
json.dump(p, open("pkg.json", "w"))
PY
check "gate passes a clean package (dry-run)" \
  'python3 skills/publish/publish.py publish --package pkg.json 2>/dev/null | grep -q DRY-RUN'

echo "== memory =="
python3 tools/memory.py add "crop reframe beats blur for talking heads" --type learning --tags campaign=t >/dev/null
python3 tools/memory.py log "smoke event" >/dev/null
check "memory store written"        '[ -s knowledge/memory/events.jsonl ]'
check "memory search finds it"      'python3 tools/memory.py search "reframe" | grep -q "crop reframe"'
check "recall shows learning+event" 'python3 tools/memory.py recall | grep -q "crop reframe" && python3 tools/memory.py recall | grep -q "smoke event"'
check "digest written"              '[ -f knowledge/MEMORY.md ]'
check "hook recall is bounded"      '[ "$(python3 tools/memory.py recall --hook | wc -l)" -le 40 ]'

echo "== content pack =="
python3 tools/content.py out/final.mp4 --brief briefs/t.json --hook "the test lounge reveal" --out out/final.content.md >/dev/null
check "content sheet has hashtags + pinned comment" \
  'grep -q "#x" out/final.content.md && grep -q "Pinned author comment" out/final.content.md'
check "content refuses banned phrases" \
  '! python3 tools/content.py out/final.mp4 --brief briefs/t.json --hook "it works fast" >/dev/null 2>&1'

echo "== progress bar render =="
python3 tools/produce.py vonly.mp4 --no-captions --grade none --out out/bar.mp4 2>/dev/null
check "bar render is 1080x1920"     'ffprobe -v error -show_entries stream=width,height -of csv=p=0 out/bar.mp4 | grep -q 1080,1920'

echo "== handoff =="
./clip handoff >/dev/null
check "HANDOFF.md written"          'grep -q "memory recall" HANDOFF.md || grep -q "Clip Factory" HANDOFF.md'

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
