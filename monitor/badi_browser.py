from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from monitor.idealista_browser import BrowserConfig, ensure_playwright_node_driver_env
from monitor.models import Listing


# Embedded JSON / SSR may include escaped slashes (handled in _room_ids_from_markup).
_ROOM_PATH_IN_MARKUP_RE = re.compile(r"/(?:es|en|ca)/room/(\d{4,})", re.IGNORECASE)
_BOT_RE = re.compile(
    r"captcha|datadome|perimeterx|px-captcha|recaptcha|hcaptcha",
    re.IGNORECASE,
)


def _normalize_badi_url(*, base_url: str, href: str) -> str | None:
    if not href:
        return None
    if href.startswith("javascript:") or href.startswith("#"):
        return None

    absolute = urljoin(base_url, href)
    p = urlparse(absolute)
    if not p.scheme.startswith("http"):
        return None
    host = (p.netloc or "").lower()
    if "badi.com" not in host:
        return None

    m = re.search(r"/(?:es|en|ca)/room/(\d{4,})", p.path or "", re.IGNORECASE)
    if not m:
        return None
    rid = m.group(1)
    return f"{p.scheme}://{p.netloc}/es/room/{rid}"


def _room_ids_from_markup(html: str) -> list[str]:
    if not html:
        return []
    text = html.replace("\\/", "/")
    seen: set[str] = set()
    out: list[str] = []
    for m in _ROOM_PATH_IN_MARKUP_RE.finditer(text):
        rid = m.group(1)
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
    return out


def _click_if_visible(page: Page, selectors: Iterable[str], *, timeout_ms: int = 1200) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() < 1:
                continue
            if not loc.first.is_visible():
                continue
            loc.first.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _handle_consent(page: Page) -> None:
    selectors = [
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("Aceptar")',
        'button:has-text("Acepto")',
        'button:has-text("Aceptar todo")',
        'button[aria-label*="Accept"]',
        'button[aria-label*="Aceptar"]',
        '[id*="didomi"] button:has-text("Aceptar")',
        '[class*="didomi"] button:has-text("Aceptar")',
        '[id*="onetrust"] button:has-text("Accept")',
    ]

    if _click_if_visible(page, selectors):
        return

    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                for sel in selectors:
                    loc = fr.locator(sel)
                    if loc.count() < 1:
                        continue
                    if not loc.first.is_visible():
                        continue
                    loc.first.click(timeout=1200)
                    return
            except Exception:
                continue
    except Exception:
        return


def _is_bot_challenge(page: Page) -> bool:
    try:
        html = page.content()
    except Exception:
        return False
    return bool(_BOT_RE.search(html))


def _should_skip_badi_after_challenge(page: Page) -> bool:
    """Non-blocking: if a captcha/bot page is detected, skip Badi so other sources can run."""
    if not _is_bot_challenge(page):
        return False
    print("Badi: captcha/bot challenge detected; skipping this source.", file=sys.stderr)
    return True


def _extract_listings_from_page(page: Page, *, source_name: str) -> list[Listing]:
    base_url = page.url
    items: dict[str, Listing] = {}

    try:
        html = page.content()
    except Exception:
        html = ""

    anchors = page.locator('a[href*="/room/"]')
    n = anchors.count()
    for i in range(n):
        a = anchors.nth(i)
        href = (a.get_attribute("href") or "").strip()
        url = _normalize_badi_url(base_url=base_url, href=href)
        if not url:
            continue

        title = (a.get_attribute("title") or "").strip()
        if not title:
            title = (a.get_attribute("aria-label") or "").strip()
        if not title:
            try:
                title = (a.inner_text() or "").strip()
            except Exception:
                title = ""
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            title = f"Badi {url.rsplit('/', 1)[-1]}"

        items[url] = Listing(source_name=source_name, url=url, title=title[:500])

    p = urlparse(base_url)
    origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else "https://badi.com"
    for rid in _room_ids_from_markup(html):
        url = f"{origin}/es/room/{rid}"
        if url not in items:
            items[url] = Listing(source_name=source_name, url=url, title=f"Badi {rid}"[:500])

    return list(items.values())


def _wait_for_results(page: Page) -> None:
    try:
        page.wait_for_selector('a[href*="/room/"]', timeout=25_000)
    except PlaywrightTimeoutError:
        pass


def _scroll_room_feed_until_stable(page: Page, *, max_rounds: int = 35) -> None:
    """
    Badi's search results usually live in an inner scroll container; window scroll alone may not load more.
    """
    stable_rounds = 0
    last_count = -1

    for _ in range(max(1, int(max_rounds))):
        _handle_consent(page)
        try:
            page.evaluate(
                """() => {
                  const a = document.querySelector('a[href*="/room/"]');
                  if (!a) { window.scrollTo(0, document.body.scrollHeight); return; }
                  let el = a;
                  while (el) {
                    const st = getComputedStyle(el);
                    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight + 40) {
                      el.scrollTop = el.scrollHeight;
                      return;
                    }
                    el = el.parentElement;
                  }
                  window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
        except Exception:
            break

        try:
            page.wait_for_timeout(450)
        except Exception:
            pass

        try:
            count = page.locator('a[href*="/room/"]').count()
        except Exception:
            count = last_count

        if count <= last_count and last_count >= 0:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = count

        if stable_rounds >= 3:
            break


def _goto_results(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    _handle_consent(page)
    try:
        page.wait_for_load_state("load", timeout=20_000)
    except PlaywrightTimeoutError:
        pass
    _handle_consent(page)
    _wait_for_results(page)


def _click_next(page: Page) -> bool:
    if _click_if_visible(page, ['a[rel="next"]']):
        return True
    if _click_if_visible(
        page,
        [
            'a:has-text("Siguiente")',
            'a:has-text("Next")',
            'button:has-text("Siguiente")',
            'button:has-text("Next")',
            '[aria-label*="Siguiente"]',
            '[aria-label*="Next"]',
            '[data-testid*="pagination-next"]',
        ],
    ):
        return True
    return False


def fetch_badi_listings_browser(
    *,
    search_url: str,
    source_name: str = "badi",
    max_pages: int = 3,
    config: BrowserConfig,
) -> list[Listing]:
    max_pages = max(1, min(int(max_pages), 50))

    ensure_playwright_node_driver_env()
    with sync_playwright() as p:
        if config.cdp_endpoint:
            browser = p.chromium.connect_over_cdp(config.cdp_endpoint)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            _goto_results(page, search_url)
            if _should_skip_badi_after_challenge(page):
                return []

            merged: dict[str, Listing] = {}
            for _ in range(max_pages):
                _handle_consent(page)
                _scroll_room_feed_until_stable(page)
                for li in _extract_listings_from_page(page, source_name=source_name):
                    merged[li.url] = li

                before = page.url
                if not _click_next(page):
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_selector('a[href*="/room/"]', timeout=20_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            if not out:
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "badi")[:40]
                html_path = debug_dir / f"{safe}.html"
                png_path = debug_dir / f"{safe}.png"
                try:
                    html_path.write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass
                try:
                    page.screenshot(path=str(png_path), full_page=True)
                except Exception:
                    pass

            return out

        launch_kwargs = dict(
            user_data_dir=config.user_data_dir,
            headless=config.headless,
            locale=config.locale,
            timezone_id=config.timezone_id,
            viewport={"width": config.viewport[0], "height": config.viewport[1]},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--profile-directory={config.profile_directory}",
            ],
        )
        if config.executable_path:
            ctx = p.chromium.launch_persistent_context(
                **launch_kwargs, executable_path=config.executable_path
            )
        else:
            try:
                ctx = p.chromium.launch_persistent_context(
                    **launch_kwargs, channel=config.browser_channel
                )
            except Exception:
                if config.force_browser_channel:
                    raise
                ctx = p.chromium.launch_persistent_context(**launch_kwargs)

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _goto_results(page, search_url)
            if _should_skip_badi_after_challenge(page):
                return []

            merged: dict[str, Listing] = {}
            for _ in range(max_pages):
                _handle_consent(page)
                _scroll_room_feed_until_stable(page)
                for li in _extract_listings_from_page(page, source_name=source_name):
                    merged[li.url] = li

                before = page.url
                if not _click_next(page):
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    page.wait_for_selector('a[href*="/room/"]', timeout=20_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            if not out:
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "badi")[:40]
                html_path = debug_dir / f"{safe}.html"
                png_path = debug_dir / f"{safe}.png"
                try:
                    html_path.write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass
                try:
                    page.screenshot(path=str(png_path), full_page=True)
                except Exception:
                    pass
            return out
        finally:
            ctx.close()
