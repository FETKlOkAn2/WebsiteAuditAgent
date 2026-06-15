"""
Annotated screenshots — the "proof" step.

A cold email that *describes* a problem ("your Book button is broken")
converts a few percent. A cold email that *shows* it — a screenshot of the
prospect's own homepage with the broken element circled in red — converts
several times higher, because the owner sees you actually spent time on
their site.

This module renders a prospect's page in a headless browser and, when we
know what to point at, highlights that element directly in the browser
(red outline + a caption banner) before capturing the viewport. Doing the
annotation in-browser avoids fragile pixel-coordinate maths and always
lines up with what the visitor really sees.

Deliverability note (important): the proof image is generated here but is
NOT meant to be attached to the first cold email — attachments/links in a
first touch hurt inbox placement. The high-converting pattern is to tease
the problem in plain text, offer the screenshot ("want me to send it?"),
and deliver it in the reply/follow-up, which is already a warm thread.

Dependencies (optional): playwright + a chromium build, and Pillow is NOT
required (annotation is done in-browser). If Playwright is unavailable the
caller should treat screenshots as a best-effort extra and continue.

    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from storage import domain_of

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = os.path.join("output", "screenshots")
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20_000
LOCATE_TIMEOUT_MS = 2_500


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass
class HighlightTarget:
    """
    What to point at on the page.

    `text_candidates` are tried in order; the first that resolves to a
    visible element wins. `caption` is the banner drawn across the top of
    the shot (kept short — it's a label, not a sentence).
    """
    text_candidates: list[str] = field(default_factory=list)
    caption: str = ""

    def is_empty(self) -> bool:
        return not any(t and t.strip() for t in self.text_candidates)


@dataclass
class ScreenshotResult:
    url: str
    path: Optional[str] = None     # file written, or None on failure
    annotated: bool = False        # did we draw a highlight?
    target_found: bool = False     # did we locate the intended element?
    error: Optional[str] = None

    def ok(self) -> bool:
        return self.path is not None and self.error is None


# ---------------------------------------------------------------------------
# In-browser annotation script
# ---------------------------------------------------------------------------

# Runs in the page. Scrolls the target into view, outlines it, and drops a
# fixed banner so the captured viewport always shows both.
_ANNOTATE_JS = """
([selectorText, caption]) => {
  // Find the first visible element whose text contains `selectorText`.
  const needle = (selectorText || '').toLowerCase().trim();
  let target = null;
  if (needle) {
    const walk = document.body ? document.body.querySelectorAll('*') : [];
    for (const el of walk) {
      if (el.children.length > 0) continue;             // leaf nodes only
      const txt = (el.textContent || '').toLowerCase().trim();
      if (!txt) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue; // not visible
      if (txt.includes(needle)) { target = el; break; }
    }
  }
  if (target) {
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.style.setProperty('outline', '4px solid #e11d48', 'important');
    target.style.setProperty('outline-offset', '3px', 'important');
    target.style.setProperty('background-color', 'rgba(225,29,72,0.08)', 'important');
  }
  if (caption) {
    const banner = document.createElement('div');
    banner.textContent = caption;
    banner.setAttribute('data-bb-banner', '1');
    Object.assign(banner.style, {
      position: 'fixed', top: '0', left: '0', right: '0',
      zIndex: '2147483647', background: '#e11d48', color: '#ffffff',
      font: '600 16px -apple-system, Segoe UI, Roboto, sans-serif',
      padding: '12px 18px', textAlign: 'center', letterSpacing: '0.2px',
      boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
    });
    document.body.appendChild(banner);
  }
  return Boolean(target);
}
"""


# ---------------------------------------------------------------------------
# Screenshotter
# ---------------------------------------------------------------------------

class PageScreenshotter:
    """
    Owns a headless chromium instance for the lifetime of a `with` block, so
    a batch of prospects reuses one browser instead of launching one each.

        with PageScreenshotter() as shot:
            for url, target in prospects:
                result = shot.capture(url, target)

    Construction is cheap; the browser launches on __enter__ and closes on
    __exit__. If Playwright isn't installed, __enter__ raises ImportError —
    callers that treat screenshots as optional should catch it.
    """

    def __init__(
        self,
        *,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        viewport: Optional[dict] = None,
        headless: bool = True,
    ) -> None:
        self._output_dir = output_dir
        self._viewport = viewport or DEFAULT_VIEWPORT
        self._headless = headless
        self._playwright = None
        self._browser = None

    def __enter__(self) -> "PageScreenshotter":
        from playwright.sync_api import sync_playwright  # local: optional dep
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        os.makedirs(self._output_dir, exist_ok=True)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

    def capture(
        self,
        url: str,
        target: Optional[HighlightTarget] = None,
        *,
        filename: Optional[str] = None,
    ) -> ScreenshotResult:
        """
        Render `url` and write a PNG. If `target` resolves to an element, it's
        highlighted and the viewport (with banner) is captured; otherwise a
        full-page screenshot is taken without annotation. Never raises for
        per-page problems — failures come back on `ScreenshotResult.error`.
        """
        if self._browser is None:
            return ScreenshotResult(url=url, error="screenshotter not started")

        result = ScreenshotResult(url=url)
        page = None
        context = None
        try:
            context = self._browser.new_context(
                viewport=self._viewport,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.set_default_timeout(LOCATE_TIMEOUT_MS)
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # Give late content a moment without hanging forever.
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass

            full_page = True
            if target and not target.is_empty():
                found = self._annotate(page, target)
                result.target_found = bool(found)
                result.annotated = bool(found)
                # When we found + highlighted the element it's centered in the
                # viewport, so a viewport shot frames it nicely. If we didn't
                # find it, fall back to the whole page.
                full_page = not found

            path = os.path.join(self._output_dir, filename or self._default_name(url))
            page.screenshot(path=path, full_page=full_page)
            result.path = path
            return result

        except Exception as e:
            result.error = f"{e.__class__.__name__}: {e}"
            logger.warning(f"screenshot failed for {url}: {result.error}")
            return result
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    # -- internals ---------------------------------------------------------

    def _annotate(self, page, target: HighlightTarget) -> bool:
        """Try each text candidate; highlight the first that resolves. Returns
        True if an element was highlighted."""
        for candidate in target.text_candidates:
            if not candidate or not candidate.strip():
                continue
            try:
                found = page.evaluate(_ANNOTATE_JS, [candidate.strip(), target.caption])
                if found:
                    return True
            except Exception:
                continue
        # Nothing matched, but still drop the banner for context.
        if target.caption:
            try:
                page.evaluate(_ANNOTATE_JS, ["", target.caption])
            except Exception:
                pass
        return False

    def _default_name(self, url: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = domain_of(url).replace(".", "_") or "page"
        return f"{slug}_{stamp}.png"


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def capture_one(
    url: str,
    target: Optional[HighlightTarget] = None,
    *,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    headless: bool = True,
) -> ScreenshotResult:
    """One-shot helper that manages the browser for a single capture."""
    try:
        with PageScreenshotter(output_dir=output_dir, headless=headless) as shot:
            return shot.capture(url, target)
    except ImportError:
        return ScreenshotResult(url=url, error="playwright not installed")


# ---------------------------------------------------------------------------
# CLI — quick manual check on a real site
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Capture an (optionally annotated) screenshot.")
    p.add_argument("url")
    p.add_argument("--highlight", default="", help="text on the page to circle in red")
    p.add_argument("--caption", default="", help="banner text across the top")
    p.add_argument("--out-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--headed", action="store_true", help="show the browser window")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    url = args.url if args.url.startswith("http") else "https://" + args.url
    tgt = None
    if args.highlight or args.caption:
        tgt = HighlightTarget(
            text_candidates=[args.highlight] if args.highlight else [],
            caption=args.caption,
        )
    res = capture_one(url, tgt, output_dir=args.out_dir, headless=not args.headed)
    if res.ok():
        print(f"  saved: {res.path}  (annotated={res.annotated}, target_found={res.target_found})")
    else:
        print(f"  FAILED: {res.error}")
