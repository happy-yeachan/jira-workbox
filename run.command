#!/bin/bash
# macOS launcher: double-click in Finder, or run ./run.command
# (chmod +x run.command if Finder refuses to open it)
#
# Needs nothing pre-installed but uv — and it installs that for you if missing.
# uv then fetches the right Python and all dependencies from uv.lock on first run.
set -u
cd "$(dirname "$0")" || exit 1

HOST="${WORKBOX_HOST:-127.0.0.1}"
PORT="${WORKBOX_PORT:-8000}"

ensure_uv() {
  command -v uv >/dev/null 2>&1 && return 0
  # uv may already be installed but not yet on this shell's PATH
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 && return 0
  echo "uv가 없어 자동 설치를 시도합니다… (astral.sh 공식 설치 스크립트)"
  curl -LsSf https://astral.sh/uv/install.sh | sh || true
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1
}

if ! ensure_uv; then
  echo
  echo "uv 자동 설치에 실패했습니다(네트워크 차단 등). 아래를 직접 실행한 뒤 다시 열어주세요:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  read -r -p "Enter를 눌러 닫기..." _
  exit 1
fi

# Credentials live in the macOS keychain, not in this folder. If none are stored
# yet, the server still starts and the page shows a setup form to enter them.
echo "의존성 준비 중… (처음 한 번은 파이썬·패키지를 받느라 조금 걸립니다)"

# Open the browser slightly ahead of the server; the first load may need a refresh.
( sleep 2; open "http://${HOST}:${PORT}/" ) &

exec uv run uvicorn app:app --host "$HOST" --port "$PORT"
