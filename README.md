## rental-browser-monitor

Local rental listing monitor that uses a real browser (Playwright + Chromium) to fetch listing sites like a human (Idealista + Fotocasa), dedupes results locally, and emails you new listings.

### Setup

```bash
cd "rental-browser-monitor"
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Edit `.env` and set:

- `RESEND_API_KEY`
- `RESEND_FROM`
- `EMAIL_TO`
- `IDEALISTA_SEARCH_URL`
- `FOTOCASA_SEARCH_URL` (optional)

### Run

Headful dry-run (recommended first run to handle consent/captcha and warm the profile):

```bash
python -m monitor once --headful --dry-run --max-pages 3
```

Headless run (sends email + appends to CSV):

```bash
python -m monitor once --headless --max-pages 3
```

### Run using your existing Chrome (real bookmarks/history)

If you want the automation to look as close as possible to your real browsing, start a **real Google Chrome** instance with remote debugging and your **real user data dir**, then connect to it.

1) Quit Chrome completely.

2) Start Chrome with a debugging port:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

3) In another terminal, run the monitor attaching to that Chrome:

```bash
python -m monitor once --dry-run --max-pages 3 --cdp-endpoint http://127.0.0.1:9222
```

### Run it now (copy/paste)

#### 1) Start a CDP Chrome instance (port 9222)

If it’s not already running, start it:

```bash
pkill -f "Google Chrome" 2>/dev/null
pkill -f "chrome_crashpad_handler" 2>/dev/null
mkdir -p "$HOME/tmp"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/tmp/chrome-cdp-profile" \
  --no-first-run \
  --no-default-browser-check
```

Leave that terminal open. Verify:

```bash
curl -s http://127.0.0.1:9222/json/version
```

#### 2) Run the monitor once

Dry-run (prints listings only):

```bash
cd "/Users/user/Projects/untitled folder/rental-browser-monitor"
source .venv/bin/activate
python -m monitor once --headful --dry-run --max-pages 3 --cdp-endpoint http://127.0.0.1:9222
```

Send email + update `data/notified.csv`:

```bash
python -m monitor once --headful --max-pages 3 --cdp-endpoint http://127.0.0.1:9222
```

#### 3) Run every 30s with timestamps

```bash
while true; do
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') starting run ====="
  python -m monitor once --headful --max-pages 3 --cdp-endpoint http://127.0.0.1:9222
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') done; sleeping 30s ====="
  sleep 30
done
```

### Parallel instances

Use a different user data dir + CSV per instance:

```bash
python -m monitor once --headless --user-data-dir data/user-data-2 --csv data/notified-2.csv
```

### Troubleshooting

- If you hit a bot challenge (DataDome CAPTCHA), rerun with `--headful` using the same `--user-data-dir` and solve it once. The run will pause and ask you to press Enter after solving. Subsequent headless runs may reuse that profile.
# rental-browser-monitor
# rental-browser-monitor
