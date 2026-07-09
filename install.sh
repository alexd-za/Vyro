#!/usr/bin/env bash
# install.sh — one command from fresh machine to working Clip Factory.
#   ./install.sh          interactive (asks before sudo / optional extras)
#   ./install.sh --yes    non-interactive: install everything it can
# Installs: ffmpeg + python3 (system), a project venv, optional extras
# (faster-whisper for captions/transcripts, pillow for covers), then runs doctor.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

YES=0; [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] && YES=1
if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; O=$'\e[38;5;209m'; B=$'\e[1m'; D=$'\e[2m'; X=$'\e[0m'
else G=; R=; Y=; O=; B=; D=; X=; fi

step(){ printf "\n${O}${B}▸ %s${X}\n" "$1"; }
ok(){   printf "  ${G}✓${X} %s\n" "$1"; }
skip(){ printf "  ${D}· %s${X}\n" "$1"; }
die(){  printf "  ${R}✗ %s${X}\n" "$1"; exit 1; }
ask(){  [ $YES -eq 1 ] && return 0; read -rp "  $1 [Y/n] " a; [ "${a:-y}" != "n" ]; }
have(){ command -v "$1" >/dev/null 2>&1; }

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

printf "${O}"
cat <<'ART'
    ___ _    ___ ___   ___ _   ___ _____ ___  _____   __
   / __| |  |_ _| _ \ | __/_\ / __|_   _/ _ \| _ \ \ / /
  | (__| |__ | ||  _/ | _/ _ \ (__  | || (_) |   /\ V /
   \___|____|___|_|   |_/_/ \_\___| |_| \___/|_|_\ |_|
ART
printf "${X}  ${D}installer — approved footage → clips that earn${X}\n"

step "System packages (ffmpeg, python3)"
NEED=()
have ffmpeg  || NEED+=(ffmpeg)
have python3 || NEED+=(python3)
if [ ${#NEED[@]} -eq 0 ]; then
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}') · python $(python3 -V 2>&1 | awk '{print $2}') already installed"
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
[ -f setup-agents.sh ] && bash setup-agents.sh >/dev/null
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && ok "created .env from template"; }
if [ ! -d .venv ]; then spin "create python venv" python3 -m venv .venv || die "venv failed"; else skip "venv exists"; fi
PIP=".venv/bin/pip"
spin "upgrade pip" "$PIP" install -q --upgrade pip || true

step "Optional extras"
if .venv/bin/python -c 'import faster_whisper' 2>/dev/null; then skip "faster-whisper installed"
elif ask "install faster-whisper? (word-synced captions + transcripts, ~250 MB)"; then
  spin "pip install faster-whisper" "$PIP" install -q faster-whisper || true
else skip "skipped faster-whisper — ./clip produce renders without captions"; fi
if .venv/bin/python -c 'import PIL' 2>/dev/null; then skip "pillow installed"
elif ask "install pillow? (cover image generator, small)"; then
  spin "pip install pillow" "$PIP" install -q pillow || true
else skip "skipped pillow — ./clip cover won't run"; fi

step "Health check"
chmod +x clip
./clip doctor

printf "${B}Done.${X} Drop videos in ${B}inbox/${X} then run ${B}./clip${X} — or hand the folder to your AI agent.\n"
