import base64
import io
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from pypdf import PdfReader

_CANVAS_SELECTOR = 'canvas[data-testid="preview-canvas-primary"]'
_CAPTURE_DEVICE_SCALE = 4.2
_PDF_PAGE_WIDTH_PT = 720
_PDF_PAGE_HEIGHT_PT = 1018.08
_PDF_PAGE_WIDTH_IN = _PDF_PAGE_WIDTH_PT / 72
_PDF_PAGE_HEIGHT_IN = _PDF_PAGE_HEIGHT_PT / 72
_SCREENSHOT_MIN_BYTES = 5000


@dataclass
class ResumeioBrowserRenderer:
    """Render an authenticated Resume.io preview page to PDF."""

    preview_url: str
    filename: str = "resume.pdf"
    wait_selector: Optional[str] = None
    timeout_ms: int = 45000
    max_pages: int = 20

    def __post_init__(self) -> None:
        self.page_count = 0
        self.render_status = ""

    def generate_pdf(self) -> bytes:
        self.__validate_preview_url()
        cookie_header = os.getenv("RESUMEIO_COOKIE", "").strip()
        if not cookie_header:
            raise HTTPException(
                status_code=400,
                detail="RESUMEIO_COOKIE is required for browser rendering",
            )

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Playwright is not installed. Install dependencies and run "
                    "`python -m playwright install chromium`."
                ),
            ) from exc

        try:
            with sync_playwright() as playwright:
                self.__log("launching chromium")
                browser = playwright.chromium.launch(
                    args=[
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--window-size=1440,1400",
                    ],
                    headless=True,
                )
                try:
                    context = self.__new_context(browser, self.__parse_cookie_header(cookie_header))
                    page = context.new_page()
                    self.__prepare_page(page)
                    self.__open_preview(page, PlaywrightError)
                    self.__raise_if_signed_out(page)

                    if self.wait_selector:
                        self.__log(f"waiting for caller selector: {self.wait_selector}")
                        page.wait_for_selector(self.wait_selector, timeout=self.timeout_ms)

                    screenshots = self.__capture_resume_pages(page)
                    if not screenshots:
                        self.__raise_blank_canvas_error(page)

                    pdf = self.__build_pdf(context, screenshots)
                finally:
                    self.__log("closing chromium")
                    browser.close()
        except PlaywrightError as exc:
            raise HTTPException(status_code=502, detail=f"Browser render failed: {exc}") from exc

        self.page_count = len(PdfReader(io.BytesIO(pdf)).pages)
        self.__log(f"finished PDF with {self.page_count} page(s)")
        return pdf

    def __new_context(self, browser, cookies: list[dict[str, str]]):
        self.__log("creating browser context")
        context = browser.new_context(
            viewport={"width": 1440, "height": 1400},
            device_scale_factor=_CAPTURE_DEVICE_SCALE,
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        context.add_cookies(cookies)
        return context

    def __prepare_page(self, page) -> None:
        page.set_default_timeout(self.timeout_ms)
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def __open_preview(self, page, playwright_error) -> None:
        self.__log(f"opening preview url with timeout {self.timeout_ms}ms")
        page.goto(self.preview_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except playwright_error:
            self.__log("network did not become idle after domcontentloaded; continuing")
        page.wait_for_timeout(2500)

    def __raise_if_signed_out(self, page) -> None:
        current_url = page.url
        if "/auth/sign-in" not in current_url:
            return

        self.__log(f"resume.io redirected to sign-in page: {current_url}")
        raise HTTPException(
            status_code=401,
            detail=(
                "Resume.io redirected to the sign-in page. Refresh RESUMEIO_COOKIE in .env "
                "with cookies from a browser session that is logged in to Resume.io, then "
                "recreate the Docker container."
            ),
        )

    def __capture_resume_pages(self, page) -> list[bytes]:
        self.__wait_for_canvas(page)
        total_pages = self.__detect_total_pages(page)
        screenshots = []

        self.__log("capturing resume page 1")
        first_page = self.__screenshot_visible_canvas(page)
        if first_page:
            screenshots.append(first_page)

        for page_index in range(2, total_pages + 1):
            if not self.__click_next_page(page):
                self.__log(f"next-page button not found before page {page_index}; stopping capture")
                break

            page.wait_for_timeout(1500)
            self.__log(f"capturing resume page {page_index}")
            screenshot = self.__screenshot_visible_canvas(page)
            if screenshot:
                screenshots.append(screenshot)

        return screenshots

    def __wait_for_canvas(self, page) -> None:
        try:
            self.__log("waiting for resume preview canvas")
            page.wait_for_selector(_CANVAS_SELECTOR, timeout=min(self.timeout_ms, 15000))
            page.wait_for_timeout(2000)
        except Exception:
            self.__log("resume preview canvas was not found before timeout")

    def __detect_total_pages(self, page) -> int:
        total_pages = page.evaluate(
            """
            (maxPages) => {
                const candidates = [];
                function collect(root) {
                    for (const el of root.querySelectorAll('*')) {
                        const txt = (el.textContent || '').trim();
                        const m = txt.match(/^(\\d+)\\s*\\/\\s*(\\d+)$/);
                        if (m) {
                            const current = parseInt(m[1]);
                            const total = parseInt(m[2]);
                            if (current >= 1 && total >= current && total <= maxPages) {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                const visible = style.display !== 'none' && style.visibility !== 'hidden'
                                    && rect.width > 0 && rect.height > 0;
                                if (visible) {
                                    let score = 0;
                                    if (rect.width <= 160 && rect.height <= 80) score += 2;
                                    if (rect.left > window.innerWidth / 2) score += 1;
                                    if (rect.top > window.innerHeight / 2) score += 1;
                                    candidates.push({ total, score });
                                }
                            }
                        }
                        if (el.shadowRoot) collect(el.shadowRoot);
                    }
                }
                collect(document);
                candidates.sort((a, b) => b.score - a.score);
                return candidates.length ? candidates[0].total : 1;
            }
            """,
            self.max_pages,
        ) or 1

        if total_pages < 1 or total_pages > self.max_pages:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Browser renderer found an invalid or unsupported page count "
                    f"({total_pages}). Set max_pages higher if this is expected."
                ),
            )

        self.__log(f"detected {total_pages} resume page(s)")
        return total_pages

    def __screenshot_visible_canvas(self, page) -> bytes | None:
        self.__hide_nav_overlays(page)
        handles = page.locator(_CANVAS_SELECTOR).all()
        for handle in handles:
            try:
                handle.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                screenshot = handle.screenshot(type="png", timeout=min(self.timeout_ms, 10000))
                if screenshot and len(screenshot) > _SCREENSHOT_MIN_BYTES:
                    return screenshot
            except Exception as exc:
                self.__log(f"canvas screenshot failed: {exc}")
        return None

    def __hide_nav_overlays(self, page) -> None:
        page.evaluate(
            """
            () => {
                function hide(root) {
                    root.querySelectorAll('*').forEach(el => {
                        try {
                            const style = window.getComputedStyle(el);
                            if (style.position !== 'absolute' && style.position !== 'fixed') return;
                            const txt = (el.textContent || '').trim();
                            if (txt.length < 15 && /\\d+\\s*\\/\\s*\\d+/.test(txt)) {
                                el.style.setProperty('display', 'none', 'important');
                            }
                        } catch(_) {}
                        if (el.shadowRoot) hide(el.shadowRoot);
                    });
                }
                hide(document);
            }
            """,
        )

    def __click_next_page(self, page) -> bool:
        return bool(
            page.evaluate(
                """
                () => {
                    function findInRoot(root) {
                        for (const btn of root.querySelectorAll('button')) {
                            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const txt = btn.textContent?.trim() || '';
                            if (label.includes('next') || txt === '>' || txt === '\\u203a') {
                                if (!btn.disabled) { btn.click(); return true; }
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot && findInRoot(el.shadowRoot)) return true;
                        }
                        return false;
                    }
                    return findInRoot(document);
                }
                """,
            ),
        )

    def __build_pdf(self, context, screenshots: list[bytes]) -> bytes:
        images = "".join(
            f'<img src="data:image/png;base64,{base64.b64encode(screenshot).decode()}">'
            for screenshot in screenshots
        )
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
@page {{ size: {_PDF_PAGE_WIDTH_PT}pt {_PDF_PAGE_HEIGHT_PT}pt; margin: 0; }}
html, body {{ margin: 0; padding: 0; background: white; }}
img {{
    display: block;
    width: {_PDF_PAGE_WIDTH_PT}pt;
    height: {_PDF_PAGE_HEIGHT_PT}pt;
    object-fit: contain;
    page-break-after: always;
    break-after: page;
}}
</style></head><body>{images}</body></html>"""

        self.__log(f"building PDF from {len(screenshots)} captured page(s)")
        pdf_page = context.new_page()
        pdf_page.set_content(html, wait_until="domcontentloaded")
        self.render_status = f"canvas-screenshot:{len(screenshots)}"
        return pdf_page.pdf(
            width=f"{_PDF_PAGE_WIDTH_IN}in",
            height=f"{_PDF_PAGE_HEIGHT_IN}in",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )

    def __raise_blank_canvas_error(self, page) -> None:
        screenshot = base64.b64encode(page.screenshot(full_page=True)).decode()
        self.__log(f"preview canvas missing at url: {page.url}")
        raise HTTPException(
            status_code=502,
            detail=(
                "Preview canvas not found or blank after waiting. "
                f"Screenshot: data:image/png;base64,{screenshot[:300]}..."
            ),
        )

    def __validate_preview_url(self) -> None:
        parsed = urlparse(self.preview_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or (host != "resume.io" and not host.endswith(".resume.io")):
            raise HTTPException(
                status_code=400,
                detail="preview_url must be an https://resume.io URL",
            )

    def __parse_cookie_header(self, cookie_header: str) -> list[dict[str, str]]:
        parsed = urlparse(self.preview_url)
        cookie_url = f"{parsed.scheme}://{parsed.hostname}"
        cookies = []
        for cookie in cookie_header.split(";"):
            if "=" not in cookie:
                continue

            name, value = cookie.strip().split("=", 1)
            if not name:
                continue

            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "url": cookie_url,
                },
            )

        if not cookies:
            raise HTTPException(
                status_code=400,
                detail="RESUMEIO_COOKIE did not contain any valid cookies",
            )

        return cookies

    def __log(self, message: str) -> None:
        print(f"[browser-render] {message}", flush=True)
