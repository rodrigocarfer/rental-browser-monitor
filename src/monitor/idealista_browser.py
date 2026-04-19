from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from monitor.models import Listing


_INMUEBLE_RE = re.compile(r"^/inmueble/(\d+)(/|$)")


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    user_data_dir: str
    locale: str = "es-ES"
    timezone_id: str = "Europe/Madrid"
    viewport: tuple[int, int] = (1365, 768)


def _normalize_idealista_url(*, base_url: str, href: str) -> str | None:
    if not href:
        return None
    if href.startswith("javascript:") or href.startswith("#"):
        return None

    absolute = urljoin(base_url, href)
    p = urlparse(absolute)
    if not p.scheme.startswith("http"):
        return None
    if "idealista." not in (p.netloc or ""):
        return None

    m = _INMUEBLE_RE.match(p.path or "")
    if not m:
        return None
    pid = m.group(1)
    return f"{p.scheme}://{p.netloc}/inmueble/{pid}/"


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
    # Best-effort; Idealista consent UIs vary. We'll try a few common patterns.
    _click_if_visible(
        page,
        selectors=[
            'button:has-text("Aceptar")',
            'button:has-text("Acepto")',
            'button:has-text("Aceptar y cerrar")',
            'button:has-text("Aceptar todo")',
            'button:has-text("Aceptar todas")',
            '[id*="didomi"] button:has-text("Aceptar")',
            'button[aria-label*="Aceptar"]',
        ],
    )


def _extract_listings_from_page(page: Page, *, source_name: str) -> list[Listing]:
    base_url = page.url
    items: dict[str, Listing] = {}

    anchors = page.locator('a[href*="/inmueble/"]')
    n = anchors.count()
    for i in range(n):
        a = anchors.nth(i)
        href = (a.get_attribute("href") or "").strip()
        url = _normalize_idealista_url(base_url=base_url, href=href)
        if not url:
            continue

        title = (a.get_attribute("title") or "").strip()
        if not title:
            try:
                title = (a.inner_text() or "").strip()
            except Exception:
                title = ""
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            title = f"Idealista {url.rsplit('/', 2)[-2]}"

        items[url] = Listing(source_name=source_name, url=url, title=title[:500])

    return list(items.values())


def _goto_results(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    _handle_consent(page)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeoutError:
        # network can stay chatty; proceed with DOM-based extraction
        pass


def _click_next(page: Page) -> bool:
    # Try semantic rel=next first, then common pagination patterns.
    if _click_if_visible(page, ['a[rel="next"]']):
        return True
    if _click_if_visible(
        page,
        [
            'a:has-text("Siguiente")',
            'a[aria-label*="Siguiente"]',
            'li[class*="next"] a',
        ],
    ):
        return True
    return False


def fetch_idealista_listings_browser(
    *,
    search_url: str,
    source_name: str = "idealista",
    max_pages: int = 3,
    config: BrowserConfig,
) -> list[Listing]:
    """
    Use a real Chromium session (Playwright) to scrape Idealista search results.

    Notes:
    - Uses a persistent profile at `config.user_data_dir` so headful can solve consent/captcha once.
    - DOM-based extraction of listing links; avoids Idealista private APIs.
    """
    max_pages = max(1, min(int(max_pages), 50))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=config.user_data_dir,
            headless=config.headless,
            locale=config.locale,
            timezone_id=config.timezone_id,
            viewport={"width": config.viewport[0], "height": config.viewport[1]},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _goto_results(page, search_url)

            merged: dict[str, Listing] = {}
            for _ in range(max_pages):
                _handle_consent(page)
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
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    # Next didn't navigate (disabled or blocked)
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            return out
        finally:
            ctx.close()

