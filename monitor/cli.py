from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from monitor.dedupe_csv import append_notified, filter_new_listings, load_notified_urls
from monitor.email_resend import send_listings_resend
from monitor.badi_browser import fetch_badi_listings_browser
from monitor.fotocasa_browser import fetch_fotocasa_listings_browser
from monitor.idealista_browser import BrowserConfig, fetch_idealista_listings_browser
from monitor.models import Listing
from monitor.yaencontre_browser import fetch_yaencontre_listings_browser


def _default_search_url() -> str:
    return (os.environ.get("IDEALISTA_SEARCH_URL") or "https://www.idealista.com/").strip()


def _default_fotocasa_url() -> str:
    return (os.environ.get("FOTOCASA_SEARCH_URL") or "").strip()


def _default_badi_url() -> str:
    return (os.environ.get("BADI_SEARCH_URL") or "").strip()


def _default_yaencontre_url() -> str:
    return (os.environ.get("YAENCONTRE_SEARCH_URL") or "").strip()


_DEFAULT_HEARTBEAT_URL = "https://hc-ping.com/5843b6e3-9bd7-4ac0-8eaf-f3a168d40bf0"


def _send_heartbeat() -> None:
    """Ping healthchecks.io (or similar) so a successful run is visible; failures are ignored."""
    from urllib.error import URLError
    from urllib.request import urlopen

    raw = os.environ.get("HEARTBEAT_URL")
    if raw is None:
        url = _DEFAULT_HEARTBEAT_URL
    else:
        url = raw.strip()
        if not url:
            return
    try:
        with urlopen(url, timeout=15) as resp:
            resp.read(64)
    except (OSError, URLError, TimeoutError):
        pass


def _chrome_running() -> bool:
    # macOS: `pgrep -x "Google Chrome"` returns 0 if running
    try:
        r = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def _seed_profile(*, seed_profile_dir: Path, user_data_dir: Path) -> str:
    """
    Seed Playwright's user data dir using an existing Chrome profile directory.

    `seed_profile_dir` should be something like:
      ~/Library/Application Support/Google/Chrome/Default
      ~/Library/Application Support/Google/Chrome/Profile 1

    We copy:
    - the profile directory itself into `user_data_dir/<profile_name>`
    - sibling `Local State` (if present) into `user_data_dir/Local State`
    """
    if not seed_profile_dir.is_dir():
        raise FileNotFoundError(f"seed profile dir not found: {seed_profile_dir}")

    profile_name = seed_profile_dir.name
    chrome_root = seed_profile_dir.parent

    user_data_dir.mkdir(parents=True, exist_ok=True)

    local_state_src = chrome_root / "Local State"
    local_state_dst = user_data_dir / "Local State"
    if local_state_src.is_file() and not local_state_dst.exists():
        shutil.copy2(local_state_src, local_state_dst)

    dest_profile_dir = user_data_dir / profile_name
    if not dest_profile_dir.exists():
        shutil.copytree(seed_profile_dir, dest_profile_dir, dirs_exist_ok=False)

    return profile_name


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Browser-based rental listing monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("once", help="Run a single poll")
    once.add_argument(
        "--url",
        default=None,
        help="Idealista search URL (default: IDEALISTA_SEARCH_URL). Alias for --idealista-url.",
    )
    once.add_argument("--idealista-url", default=None, help="Idealista search URL (default: IDEALISTA_SEARCH_URL)")
    once.add_argument("--fotocasa-url", default=None, help="Fotocasa search URL (default: FOTOCASA_SEARCH_URL)")
    once.add_argument("--badi-url", default=None, help="Badi search URL (default: BADI_SEARCH_URL)")
    once.add_argument(
        "--yaencontre-url",
        default=None,
        help="YaEncontre search URL (default: YAENCONTRE_SEARCH_URL)",
    )
    once.add_argument("--max-pages", type=int, default=3, help="Max result pages to traverse (default: 3)")
    once.add_argument("--csv", type=Path, default=Path("data/notified.csv"), help="CSV path for dedupe")
    once.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path("data/user-data"),
        help="Playwright persistent profile directory (use different one for parallel instances)",
    )
    once.add_argument(
        "--seed-profile-dir",
        type=Path,
        default=None,
        help=(
            "Path to an existing Chrome profile directory to copy from (e.g. "
            "'~/Library/Application Support/Google/Chrome/Default'). "
            "Chrome must be closed while seeding."
        ),
    )
    once.add_argument(
        "--ignore-chrome-running",
        action="store_true",
        help="Allow seeding even if Chrome appears to be running (not recommended).",
    )
    once.add_argument(
        "--browser-channel",
        default="chrome",
        choices=["chrome", "msedge", "chromium"],
        help="Playwright browser channel (default: chrome).",
    )
    once.add_argument(
        "--force-browser-channel",
        action="store_true",
        help="Fail if the requested channel can't be launched (no fallback).",
    )
    once.add_argument(
        "--chrome-executable",
        type=str,
        default=None,
        help="Explicit Chrome/Chromium executable path (overrides --browser-channel).",
    )
    once.add_argument(
        "--cdp-endpoint",
        type=str,
        default=None,
        help=(
            "Connect to an already-running Chrome via CDP, e.g. http://127.0.0.1:9222. "
            "Use this to drive your existing browser profile (bookmarks/history) to reduce blocking."
        ),
    )
    mode = once.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="Run headless (default)")
    mode.add_argument("--headful", action="store_true", help="Run with visible browser window")
    once.add_argument("--dry-run", action="store_true", help="Do not email or write CSV; just print new listings")
    once.set_defaults(func=_cmd_once)

    return p.parse_args(argv)


def _cmd_once(args: argparse.Namespace) -> int:
    load_dotenv()

    idealista_url = (args.idealista_url or args.url or _default_search_url()).strip()
    fotocasa_url = (args.fotocasa_url or _default_fotocasa_url()).strip()
    badi_url = (args.badi_url or _default_badi_url()).strip()
    yaencontre_url = (args.yaencontre_url or _default_yaencontre_url()).strip()

    if not idealista_url and not fotocasa_url and not badi_url and not yaencontre_url:
        print(
            "Missing URLs. Set IDEALISTA_SEARCH_URL, FOTOCASA_SEARCH_URL, BADI_SEARCH_URL, "
            "and/or YAENCONTRE_SEARCH_URL, or pass --idealista-url / --fotocasa-url / --badi-url / "
            "--yaencontre-url.",
            file=sys.stderr,
        )
        return 2

    headless = True
    if args.headful:
        headless = False
    if args.headless:
        headless = True

    profile_directory = "Default"
    if args.seed_profile_dir is not None:
        if _chrome_running() and not args.ignore_chrome_running:
            print(
                "Google Chrome appears to be running. Close Chrome and re-run (to avoid profile lock/corruption), "
                "or pass --ignore-chrome-running.",
                file=sys.stderr,
            )
            return 2
        try:
            profile_directory = _seed_profile(
                seed_profile_dir=Path(args.seed_profile_dir).expanduser(),
                user_data_dir=Path(args.user_data_dir).expanduser(),
            )
            print(
                f"Seeded user data dir {args.user_data_dir} from {args.seed_profile_dir} "
                f"(profile_directory={profile_directory!r})."
            )
        except Exception as e:
            print(f"Failed to seed profile: {e}", file=sys.stderr)
            return 2

    cfg = BrowserConfig(
        headless=headless,
        user_data_dir=str(Path(args.user_data_dir).expanduser()),
        profile_directory=profile_directory,
        browser_channel=str(args.browser_channel),
        force_browser_channel=bool(args.force_browser_channel),
        executable_path=str(args.chrome_executable) if args.chrome_executable else None,
        cdp_endpoint=str(args.cdp_endpoint).strip() if args.cdp_endpoint else None,
    )

    try:
        scraped: list[Listing] = []
        if idealista_url:
            scraped += fetch_idealista_listings_browser(
                search_url=idealista_url,
                source_name="idealista",
                max_pages=int(args.max_pages),
                config=cfg,
            )
        if fotocasa_url:
            scraped += fetch_fotocasa_listings_browser(
                search_url=fotocasa_url,
                source_name="fotocasa",
                max_pages=int(args.max_pages),
                config=cfg,
            )
        if badi_url:
            scraped += fetch_badi_listings_browser(
                search_url=badi_url,
                source_name="badi",
                max_pages=int(args.max_pages),
                config=cfg,
            )
        if yaencontre_url:
            scraped += fetch_yaencontre_listings_browser(
                search_url=yaencontre_url,
                source_name="yaencontre",
                max_pages=int(args.max_pages),
                config=cfg,
            )

        if not scraped:
            print(
                "Got 0 listings from browser. A debug snapshot may have been saved to data/debug/ "
                "(often caused by consent/captcha/bot challenge). Try --headful with the same "
                "--user-data-dir and interact once.",
                file=sys.stderr,
            )

        notified = load_notified_urls(args.csv)
        new_listings = filter_new_listings(listings=scraped, notified_urls=notified)

        if not new_listings:
            print("No new listings.")
            return 0

        for li in new_listings:
            print(f"[{li.source_name}] {li.title}\n  {li.url}")

        if args.dry_run:
            print(f"Dry run: {len(new_listings)} new listing(s); no email sent, CSV not updated.")
            return 0

        send_listings_resend(listings=new_listings)
        append_notified(csv_path=args.csv, listings=new_listings)
        print(f"Sent email with {len(new_listings)} new listing(s); appended to {args.csv}.")
        return 0
    finally:
        _send_heartbeat()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return int(args.func(args))

