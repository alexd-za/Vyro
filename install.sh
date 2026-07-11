#!/usr/bin/env bash
# install.sh — one command from fresh machine to working Clip Factory.
#   ./install.sh          interactive (asks before sudo / optional extras)
#   ./install.sh --yes    non-interactive: install everything it can
# Installs: ffmpeg + python3 (system), a project venv, optional extras
# (faster-whisper for captions/transcripts, pillow for covers), then runs doctor.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

YES=0; [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] && YES=1
# light/dark-safe palette + NO_COLOR standard support
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[38;5;209m'; O=$'\e[38;5;209m'; B=$'\e[1m'; D=$'\e[38;5;245m'; X=$'\e[0m'
else G=; R=; Y=; O=; B=; D=; X=; fi
UTF8=1; case "${LC_ALL:-${LANG:-}}" in *[Uu][Tt][Ff]*8*) ;; *) UTF8=0 ;; esac
DONE_LIST=(); SKIP_LIST=()
note_done(){ DONE_LIST+=("$1"); }
note_skip(){ SKIP_LIST+=("$1"); }

step(){  # orange rule + step title (matches the ./clip menu styling)
  local r='─' t="$1"; [ "$UTF8" = 1 ] || r='-'
  printf "\n${O}%s%s${X} ${B}%s${X} ${O}%s${X}\n" "$r" "$r" "$t" \
    "$(printf "$r%.0s" $(seq 1 $((44-${#t}))))"
}
ok(){   printf "  ${G}✓${X} %s\n" "$1"; }
skip(){ printf "  ${D}· %s${X}\n" "$1"; }
die(){  printf "  ${R}✗ %s${X}\n" "$1"; exit 1; }
ask(){  [ $YES -eq 1 ] && return 0; read -rp "  $1 [Y/n] " a; [ "${a:-y}" != "n" ]; }
have(){ command -v "$1" >/dev/null 2>&1; }

seed_knowledge(){  # idempotent knowledge-base seed (was setup-agents.sh)
  mkdir -p skills knowledge work out
  [ -f knowledge/INDEX.md ] || printf '# Knowledge index\n\n- learnings.md — why clips won or lost\n- ledger.md — every shipped clip + its live URL + views\n- MEMORY.md — digest of the shared AI memory (./clip mem)\n' > knowledge/INDEX.md
  [ -f knowledge/learnings.md ] || printf '# Learnings\n\nOne line per clip that over- or under-performed, and why.\n' > knowledge/learnings.md
  [ -f knowledge/ledger.md ] || printf '# Ledger\n\n| date | campaign | clip | platform | url | views |\n|------|----------|------|----------|-----|-------|\n' > knowledge/ledger.md
}

spin(){  # spin "label" cmd...  — braille spinner while a step runs
  local label="$1"; shift
  [ -t 1 ] || { "$@" >/dev/null 2>&1; return $?; }
  local tmp pid rc i=0 f='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  case "${LC_ALL:-${LANG:-}}" in *[Uu][Tt][Ff]*8*) ;; *) f='|/-\|/-\|/' ;; esac
  tmp="$(mktemp)"; "$@" >"$tmp" 2>&1 & pid=$!
  printf '\e[?25l'
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s ' "$O" "${f:i%10:1}" "$X" "$label"; i=$((i+1)); sleep 0.08
  done
  wait "$pid"; rc=$?; printf '\r\e[?25h\e[K'
  if [ $rc -eq 0 ]; then ok "$label"; else
    printf "  ${R}✗${X} %s\n" "$label"; tail -5 "$tmp" | sed 's/^/    /'; fi
  rm -f "$tmp"; return $rc
}

banner(){
  local cols="${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}"
  case "$cols" in ''|*[!0-9]*) cols=80 ;; esac
  if [ "$UTF8" = 1 ] && [ "$cols" -ge 66 ]; then
    # block-only wordmark: solid █ glyphs render monospace in every font
    # (mixed box-drawing chars like ╗║ shear in fonts with ambiguous widths)
    local art=(
' ██████ ██      ██ ██████ '
'██      ██      ██ ██   ██'
'██      ██      ██ ██████ '
'██      ██      ██ ██     '
' ██████ ███████ ██ ██     '
'███████  █████   ██████ ████████  ██████  ██████  ██    ██'
'██      ██   ██ ██         ██    ██    ██ ██   ██  ██  ██ '
'█████   ███████ ██         ██    ██    ██ ██████    ████  '
'██      ██   ██ ██         ██    ██    ██ ██   ██    ██   '
'██      ██   ██  ██████    ██     ██████  ██   ██    ██   '
    )
    local grad=(202 202 208 208 214 214 214 220 220 220) i
    for i in "${!art[@]}"; do
      if [ -n "$O" ]; then printf '  \e[38;5;%sm%s\e[0m\n' "${grad[$i]}" "${art[$i]}"
      else printf '  %s\n' "${art[$i]}"; fi
      if [ -t 1 ]; then sleep 0.03; fi
    done
    printf "  ${D}installer — from zero to first clip${X}\n"
  elif [ "$UTF8" = 1 ]; then
    printf "  ${O}${B}▶▶ CLIP FACTORY${X} ${D}— installer — from zero to first clip${X}\n"
  else
    printf "  ${O}${B}>> CLIP FACTORY${X} ${D}-- installer: from zero to first clip${X}\n"
  fi
}
banner

step "System packages (ffmpeg, python3)"
NEED=()
have ffmpeg  || NEED+=(ffmpeg)
have python3 || NEED+=(python3)
if [ ${#NEED[@]} -eq 0 ]; then
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}') · python $(python3 -V 2>&1 | awk '{print $2}') already installed"; note_skip "system packages (already present)"
else
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  if have dnf; then
    printf "  ${D}Fedora detected — ffmpeg needs RPM Fusion${X}\n"
    ask "install ${NEED[*]} via dnf (uses $SUDO)?" || die "cannot continue without: ${NEED[*]}"
    if printf '%s\n' "${NEED[@]}" | grep -q ffmpeg; then
      spin "enable RPM Fusion" $SUDO dnf install -y \
        "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" || true
    fi
    spin "dnf install ${NEED[*]}" $SUDO dnf install -y "${NEED[@]}" python3-pip || die "dnf install failed"
  elif have apt-get; then
    ask "install ${NEED[*]} via apt (uses $SUDO)?" || die "cannot continue without: ${NEED[*]}"
    spin "apt update" $SUDO apt-get update -qq || true
    spin "apt install ${NEED[*]}" $SUDO apt-get install -y -qq "${NEED[@]}" python3-pip python3-venv || die "apt install failed"
  elif have pacman; then
    ask "install ${NEED[*]} via pacman (uses $SUDO)?" || die "cannot continue without: ${NEED[*]}"
    spin "pacman -S ${NEED[*]}" $SUDO pacman -S --noconfirm "${NEED[@]}" python-pip || die "pacman failed"
  elif have brew; then
    spin "brew install ${NEED[*]}" brew install "${NEED[@]}" || die "brew failed"
  else
    die "no supported package manager found — install manually: ${NEED[*]}"
  fi
fi

step "Project setup (folders, venv, .env)"
mkdir -p skills knowledge work out briefs inbox tools
seed_knowledge >/dev/null
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && ok "created .env from template"; }
if [ ! -d .venv ]; then spin "create python venv" python3 -m venv .venv || die "venv failed"; else skip "venv exists"; fi
PIP=".venv/bin/pip"
spin "upgrade pip" "$PIP" install -q --upgrade pip || true

step "Optional extras"
if .venv/bin/python -c 'import faster_whisper' 2>/dev/null; then skip "faster-whisper installed"
elif ask "install faster-whisper? (word-synced captions + transcripts, ~250 MB)"; then
  spin "pip install faster-whisper" "$PIP" install -q faster-whisper && note_done "faster-whisper (captions/transcripts)" || true
else skip "skipped faster-whisper — ./clip produce renders without captions"; note_skip "faster-whisper"; fi
if .venv/bin/python -c 'import PIL' 2>/dev/null; then skip "pillow installed"
elif ask "install pillow? (cover image generator, small)"; then
  spin "pip install pillow" "$PIP" install -q pillow && note_done "pillow (cover generator)" || true
else skip "skipped pillow — ./clip cover won't run"; note_skip "pillow"; fi

step "Health check"
chmod +x clip
./clip doctor

srule(){  # orange rule + UPPERCASE gray label (matches the ./clip menu styling)
  local r='─' t="$1"; [ "$UTF8" = 1 ] || r='-'
  printf "\n${O}%s%s${X} ${D}%s${X} ${O}%s${X}\n" "$r" "$r" "$t" \
    "$(printf "$r%.0s" $(seq 1 $((44-${#t}))))"
}
srule "INSTALL SUMMARY"
for d in "${DONE_LIST[@]:-}"; do [ -n "$d" ] && printf "  ${G}✓${X} %s\n" "$d"; done
for s in "${SKIP_LIST[@]:-}"; do [ -n "$s" ] && printf "  ${D}· %s${X}\n" "$s"; done
srule "NEXT STEPS"
printf "   ${B}%-14s${X}%s\n" "./clip demo" "safe end-to-end tour"
printf "   ${B}%-14s${X}%s\n" "./clip ui" "open the web dashboard"
printf "   ${B}%-14s${X}%s\n" "./clip" "the terminal menu"
