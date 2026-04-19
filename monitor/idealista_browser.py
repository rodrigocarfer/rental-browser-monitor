from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from monitor.models import Listing


_INMUEBLE_RE = re.compile(r"^/inmueble/(\d+)(/|$)")
_DATADOME_RE = re.compile(r"captcha-delivery\\.com|datadome", re.IGNORECASE)


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    user_data_dir: str
    profile_directory: str = "Default"
    browser_channel: str = "chrome"
    force_browser_channel: bool = False
    executable_path: str | None = None
    cdp_endpoint: str | None = None
    locale: str = "es-ES"
    timezone_id: str = "Europe/Madrid"
    viewport: tuple[int, int] = (1365, 768)


def ensure_playwright_node_driver_env() -> None:
    """Quiet Node deprecation spam from Playwright's bundled driver (e.g. DEP0169 `url.parse()`)."""
    flag = "--no-deprecation"
    parts = os.environ.get("NODE_OPTIONS", "").split()
    if flag not in parts:
        os.environ["NODE_OPTIONS"] = " ".join([*parts, flag]).strip()


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
    selectors = [
        # Generic Spanish accept buttons
        'button:has-text("Aceptar")',
        'button:has-text("Acepto")',
        'button:has-text("Aceptar y cerrar")',
        'button:has-text("Aceptar todo")',
        'button:has-text("Aceptar todas")',
        'button[aria-label*="Aceptar"]',
        # Didomi containers sometimes live in the main DOM
        '[id*="didomi"] button:has-text("Aceptar")',
        '[class*="didomi"] button:has-text("Aceptar")',
    ]

    if _click_if_visible(page, selectors):
        return

    # Consent banners are often inside iframes (Didomi / CMP). Try frames too.
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
        pass
    _handle_consent(page)
    try:
        page.wait_for_selector('a[href*="/inmueble/"]', timeout=20_000)
    except PlaywrightTimeoutError:
        pass


def _is_datadome(page: Page) -> bool:
    try:
        html = page.content()
    except Exception:
        return False
    return bool(_DATADOME_RE.search(html))


def _click_next(page: Page) -> bool:
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
    max_pages = max(1, min(int(max_pages), 50))

    ensure_playwright_node_driver_env()
    with sync_playwright() as p:
        if config.cdp_endpoint:
            browser = p.chromium.connect_over_cdp(config.cdp_endpoint)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _goto_results(page, search_url)
            if _is_datadome(page):
                if config.headless:
                    return []
                print(
                    "Idealista returned a DataDome CAPTCHA. Solve it in the opened browser window, "
                    "then press Enter here to continue..."
                )
                try:
                    input()
                except EOFError:
                    return []
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
                try:
                    page.wait_for_selector('a[href*="/inmueble/"]', timeout=20_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            if not out:
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "idealista")[:40]
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
            # Intentionally do not close the browser here: it's an existing Chrome instance.
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
            if _is_datadome(page):
                if config.headless:
                    return []
                # Headful: allow the user to solve the CAPTCHA once; profile is persistent.
                print(
                    "Idealista returned a DataDome CAPTCHA. Solve it in the opened browser window, "
                    "then press Enter here to continue..."
                )
                try:
                    input()
                except EOFError:
                    return []
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
                try:
                    page.wait_for_selector('a[href*="/inmueble/"]', timeout=20_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            if not out:
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "idealista")[:40]
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

