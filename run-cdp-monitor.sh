#!/usr/bin/env bash
# Launches Chrome with remote debugging, then polls Idealista/Fotocasa/Badi via Playwright CDP.
# Usage: from Terminal, run: ./run-cdp-monitor.sh
# Stop: Ctrl+C (Chrome started by this script will be terminated on exit).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDP_PORT="${CDP_PORT:-9222}"
CDP_ENDPOINT="http://127.0.0.1:${CDP_PORT}"
PROFILE_DIR="${CHROME_CDP_PROFILE_DIR:-${HOME}/tmp/chrome-cdp-profile}"
# Chrome (and GoogleUpdater) log loudly to stderr; send it here so monitor output stays readable.
# Set to /dev/null to discard, or another path to capture logs.
CHROME_STDERR_LOG="${CHROME_STDERR_LOG:-${HOME}/tmp/chrome-cdp-stderr.log}"
CHROME="${CHROME_PATH:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

if [[ ! -x "$CHROME" ]]; then
  echo "Chrome not found at: $CHROME" >&2
  echo "Set CHROME_PATH to your Google Chrome binary." >&2
  exit 1
fi

pkill -f "Google Chrome" 2>/dev/null || true
pkill -f "chrome_crashpad_handler" 2>/dev/null || true
mkdir -p "${HOME}/tmp" "$PROFILE_DIR"

echo "Starting Chrome with CDP on ${CDP_ENDPOINT} (profile: ${PROFILE_DIR})..."
echo "Chrome stderr → ${CHROME_STDERR_LOG} (TensorFlow/GCM/updater noise is normal; not from the monitor.)"
"$CHROME" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CDP_PORT}" \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  2>>"${CHROME_STDERR_LOG}" \
  &

CHROME_PID=$!

cleanup() {
  echo ""
  echo "Shutting down Chrome (pid ${CHROME_PID})..."
  kill "${CHROME_PID}" 2>/dev/null || true
  wait "${CHROME_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Waiting for CDP..."
for _ in $(seq 1 90); do
  if curl -sf "${CDP_ENDPOINT}/json/version" >/dev/null 2>&1; then
    echo "CDP is up."
    break
  fi
  if ! kill -0 "${CHROME_PID}" 2>/dev/null; then
    echo "Chrome exited before CDP became ready." >&2
    exit 1
  fi
  sleep 1
done

if ! curl -sf "${CDP_ENDPOINT}/json/version" >/dev/null 2>&1; then
  echo "Timed out waiting for CDP on ${CDP_ENDPOINT}" >&2
  exit 1
fi

cd "$SCRIPT_DIR" || exit 1
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.venv/bin/activate"

echo "Monitor loop (Ctrl+C to stop). Project: ${SCRIPT_DIR}"
echo ""

while true; do
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') Starting new run ====="
  python -m monitor once --headful --max-pages 3 --cdp-endpoint "${CDP_ENDPOINT}" || true
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') DONE; ====="
  sleep 1
done
