from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from monitor.dedupe_csv import append_notified, filter_new_listings, load_notified_urls
from monitor.email_resend import send_listings_resend
from monitor.idealista_browser import BrowserConfig, fetch_idealista_listings_browser


def _default_search_url() -> str:
    return (os.environ.get("IDEALISTA_SEARCH_URL") or "https://www.idealista.com/").strip()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Browser-based rental listing monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("once", help="Run a single poll")
    once.add_argument("--url", default=None, help="Idealista search URL (default: IDEALISTA_SEARCH_URL)")
    once.add_argument("--max-pages", type=int, default=3, help="Max result pages to traverse (default: 3)")
    once.add_argument("--csv", type=Path, default=Path("data/notified.csv"), help="CSV path for dedupe")
    once.add_argument(
        "--user-data-dir",
        type=str,
        default="data/user-data",
        help="Playwright persistent profile directory (use different one for parallel instances)",
    )
    mode = once.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="Run headless (default)")
    mode.add_argument("--headful", action="store_true", help="Run with visible browser window")
    once.add_argument("--dry-run", action="store_true", help="Do not email or write CSV; just print new listings")
    once.set_defaults(func=_cmd_once)

    return p.parse_args(argv)


def _cmd_once(args: argparse.Namespace) -> int:
    load_dotenv()

    url = (args.url or _default_search_url()).strip()
    if not url:
        print("Missing URL (set IDEALISTA_SEARCH_URL or pass --url).", file=sys.stderr)
        return 2

    headless = True
    if args.headful:
        headless = False
    if args.headless:
        headless = True

    cfg = BrowserConfig(headless=headless, user_data_dir=args.user_data_dir)

    scraped = fetch_idealista_listings_browser(
        search_url=url,
        source_name="idealista",
        max_pages=int(args.max_pages),
        config=cfg,
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return int(args.func(args))

