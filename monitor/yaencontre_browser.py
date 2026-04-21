from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from monitor.idealista_browser import BrowserConfig, ensure_playwright_node_driver_env
from monitor.models import Listing


# Detail paths: /alquiler/.../123456 (exclude search facets like /pisos/custom/.../mapa)
_DETAIL_PATH_IN_MARKUP_RE = re.compile(
    r'(/(?:alquiler|lloguer)(?:/[^/\s"<>?]+)+/\d{6,})',
    re.IGNORECASE,
)
_BOT_RE = re.compile(
    r"captcha|datadome|perimeterx|px-captcha|recaptcha|hcaptcha",
    re.IGNORECASE,
)


def _normalize_yaencontre_url(*, base_url: str, href: str) -> str | None:
    if not href:
        return None
    if href.startswith("javascript:") or href.startswith("#"):
        return None

    absolute = urljoin(base_url, href)
    p = urlparse(absolute)
    if not p.scheme.startswith("http"):
        return None
    if "yaencontre.com" not in (p.netloc or "").lower():
        return None

    path = (p.path or "").rstrip("/")
    pl = path.lower()
    if "/pisos/custom" in pl:
        return None
    if pl.endswith("/mapa") or "/o-recientes/mapa" in pl:
        return None
    if "poner-anuncio" in pl or "publicar" in pl:
        return None
    if "/alquiler/" not in pl and "/lloguer/" not in pl:
        return None

    m = re.search(r"(?:/|[-_])(\d{6,})$", path)
    if not m:
        return None

    netloc = (p.netloc or "").lower()
    return urlunparse((p.scheme, netloc, path, "", "", ""))


def _detail_paths_from_markup(html: str) -> list[str]:
    if not html:
        return []
    text = html.replace("\\/", "/")
    seen: set[str] = set()
    out: list[str] = []
    for m in _DETAIL_PATH_IN_MARKUP_RE.finditer(text):
        path = m.group(1)
        pl = path.lower()
        if "/pisos/custom" in pl or pl.endswith("/mapa"):
            continue
        if "poner-anuncio" in pl:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
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
        "#onetrust-accept-btn-handler",
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


def _should_skip_yaencontre_after_challenge(page: Page) -> bool:
    if not _is_bot_challenge(page):
        return False
    print("YaEncontre: captcha/bot challenge detected; skipping this source.", file=sys.stderr)
    return True


def _extract_listings_from_page(page: Page, *, source_name: str) -> list[Listing]:
    base_url = page.url
    items: dict[str, Listing] = {}

    try:
        html = page.content()
    except Exception:
        html = ""

    anchors = page.locator('a[href*="yaencontre"], a[href^="/alquiler/"], a[href^="/lloguer/"]')
    n = anchors.count()
    for i in range(n):
        a = anchors.nth(i)
        href = (a.get_attribute("href") or "").strip()
        url = _normalize_yaencontre_url(base_url=base_url, href=href)
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
            title = f"YaEncontre {url.rsplit('/', 1)[-1]}"

        items[url] = Listing(source_name=source_name, url=url, title=title[:500])

    p = urlparse(base_url)
    origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else "https://www.yaencontre.com"
    for path in _detail_paths_from_markup(html):
        url = _normalize_yaencontre_url(base_url=origin, href=path)
        if not url or url in items:
            continue
        items[url] = Listing(
            source_name=source_name,
            url=url,
            title=f"YaEncontre {path.rsplit('/', 1)[-1]}"[:500],
        )

    return list(items.values())


def _wait_for_results(page: Page) -> None:
    try:
        page.wait_for_selector('a[href*="/alquiler/"], a[href*="/lloguer/"]', timeout=25_000)
    except (PlaywrightTimeoutError, Exception):
        pass


def _scroll_results_until_stable(page: Page, *, max_rounds: int = 25) -> None:
    stable_rounds = 0
    last_count = -1

    for _ in range(max(1, int(max_rounds))):
        _handle_consent(page)
        try:
            page.evaluate(
                """() => {
                  const hit = document.querySelector('a[href*="/alquiler/"], a[href*="/lloguer/"]');
                  if (!hit) { window.scrollTo(0, document.body.scrollHeight); return; }
                  let el = hit;
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
            count = page.locator('a[href*="/alquiler/"], a[href*="/lloguer/"]').count()
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
        page.wait_for_load_state("load", timeout=25_000)
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
            'a:has-text("Següent")',
            'button:has-text("Siguiente")',
            '[aria-label*="Siguiente"]',
            '[data-testid*="pagination-next"]',
            'li[class*="next"] a',
        ],
    ):
        return True
    return False


def fetch_yaencontre_listings_browser(
    *,
    search_url: str,
    source_name: str = "yaencontre",
    max_pages: int = 3,
    config: BrowserConfig,
) -> list[Listing]:
    max_pages = max(1, min(int(max_pages), 50))

    ensure_playwright_node_driver_env()
    with sync_playwright() as p:
        if config.cdp_endpoint:
            browser = p.chromium.connect_over_cdp(config.cdp_endpoint)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                _goto_results(page, search_url)
                if _should_skip_yaencontre_after_challenge(page):
                    return []

                merged: dict[str, Listing] = {}
                for _ in range(max_pages):
                    _handle_consent(page)
                    _scroll_results_until_stable(page)
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
                        page.wait_for_selector('a[href*="/alquiler/"], a[href*="/lloguer/"]', timeout=20_000)
                    except PlaywrightTimeoutError:
                        pass
                    if page.url == before:
                        break

                out = list(merged.values())
                out.sort(key=lambda x: x.url)
                if not out:
                    debug_dir = Path("data/debug")
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "yaencontre")[:40]
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
            except Exception as e:
                print(f"YaEncontre scrape failed, skipping source: {e}", file=sys.stderr)
                return []
            finally:
                try:
                    page.close()
                except Exception:
                    pass

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
            if _should_skip_yaencontre_after_challenge(page):
                return []

            merged: dict[str, Listing] = {}
            for _ in range(max_pages):
                _handle_consent(page)
                _scroll_results_until_stable(page)
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
                    page.wait_for_selector('a[href*="/alquiler/"], a[href*="/lloguer/"]', timeout=20_000)
                except PlaywrightTimeoutError:
                    pass
                if page.url == before:
                    break

            out = list(merged.values())
            out.sort(key=lambda x: x.url)
            if not out:
                debug_dir = Path("data/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", "yaencontre")[:40]
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
