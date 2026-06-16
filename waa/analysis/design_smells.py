"""
Heuristic design smells (improvement #7).

The vision design critic (#6) is the deep, credible read of a page's look, but
it costs a vision call per lead. Many of the strongest "this site is dated /
amateur" signals are detectable straight from the HTML for FREE — no tokens, no
browser. A missing viewport meta tag means the site isn't responsive; <font> /
<center> / <marquee> tags, layout tables, Comic Sans, or a 1.x jQuery all date
a site by a decade. Surfacing these cheaply lets every lead carry a design
argument even when the vision critic is switched off for cost.

SOLID:
- `DesignSmellDetector` is the abstraction; each detector finds ONE smell (SRP)
  via a single `detect()` method (ISP) and is interchangeable (LSP).
- New detectors drop into the scanner without touching the others (OCP).
- Detectors read a prepared `SmellContext` (parsed soup + lowered html), so the
  HTML is parsed once and detectors depend on a small stable shape (DIP).
- `DesignSmellScanner` composes detectors; `DesignSmellReport` is the immutable
  result, and knows how to express itself as conversion-audit `Finding`s so the
  smells flow into the existing business-case / email path.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING, Optional, Sequence

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from waa.analysis.conversion_audit import Finding


_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
_SEVERITY_PENALTY = {"high": 3, "medium": 2, "low": 1}
# conversion-audit Finding confidence per smell severity
_CONFIDENCE = {"high": "high", "medium": "medium", "low": "low"}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DesignSmell:
    """One dated/amateur design signal detected from the markup."""
    code: str          # stable id, e.g. "no_viewport"
    label: str         # short human label
    severity: str      # low | medium | high
    evidence: str      # what was found on the page
    rationale: str     # why it dates the site / costs the owner

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, 0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SmellContext:
    """HTML parsed once and shared by every detector."""
    soup: BeautifulSoup
    html: str
    html_lower: str

    @classmethod
    def build(cls, html: str) -> "SmellContext":
        soup = BeautifulSoup(html or "", "lxml")
        return cls(soup=soup, html=html or "", html_lower=(html or "").lower())


@dataclass(frozen=True)
class DesignSmellReport:
    smells: tuple[DesignSmell, ...] = field(default_factory=tuple)

    def has_smells(self) -> bool:
        return bool(self.smells)

    def top(self, n: int = 3) -> list[DesignSmell]:
        return sorted(self.smells, key=lambda s: s.rank, reverse=True)[:n]

    def design_score(self) -> int:
        """0-10; 10 = no dated signals, lower = more redesign upside."""
        penalty = sum(_SEVERITY_PENALTY.get(s.severity, 0) for s in self.smells)
        return max(0, 10 - penalty)

    def labels(self) -> list[str]:
        """Short labels (highest severity first) for display / SiteFacts."""
        return [s.label for s in self.top(len(self.smells))]

    def summary_for_prompt(self) -> str:
        if not self.has_smells():
            return ""
        return "; ".join(f"{s.label} ({s.evidence})" for s in self.top(3))

    def as_findings(self) -> list["Finding"]:
        """Express the smells as conversion-audit Findings (category 'design')
        so they flow into the business-case / email path unchanged."""
        from waa.analysis.conversion_audit import Finding
        out = []
        for s in self.top(3):
            out.append(Finding(
                category="design",
                label=s.label,
                detail=s.evidence,
                confidence=_CONFIDENCE.get(s.severity, "low"),
                impact=s.rationale,
            ))
        return out

    def to_dict(self) -> dict:
        return {
            "score": self.design_score(),
            "smells": [s.to_dict() for s in self.top(len(self.smells))],
        }


# ---------------------------------------------------------------------------
# Detector abstraction + concrete detectors
# ---------------------------------------------------------------------------

class DesignSmellDetector(ABC):
    code: str = "smell"

    @abstractmethod
    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        ...


class NoViewportMeta(DesignSmellDetector):
    """No viewport meta => the site is not mobile-responsive."""
    code = "no_viewport"

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        meta = ctx.soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        if meta and (meta.get("content") or "").strip():
            return None
        return DesignSmell(
            self.code, "not mobile-responsive", "high",
            "no viewport meta tag",
            "most local searches are on a phone; a non-responsive site looks "
            "broken on mobile and visitors leave",
        )


class DeprecatedTags(DesignSmellDetector):
    """Presence tags that have been dead for ~15 years."""
    code = "deprecated_tags"
    _TAGS = ["font", "center", "marquee", "blink", "frameset", "frame", "applet"]

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        found = sorted({t.name for t in ctx.soup.find_all(self._TAGS)})
        if not found:
            return None
        return DesignSmell(
            self.code, "uses obsolete HTML tags", "high",
            "found " + ", ".join(f"<{t}>" for t in found),
            "tags like these were abandoned over a decade ago and instantly "
            "date the site to a customer's eye",
        )


class LayoutTables(DesignSmellDetector):
    """Tables used for page layout (nested tables or role=presentation)."""
    code = "layout_tables"

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        tables = ctx.soup.find_all("table")
        if not tables:
            return None
        presentation = any(
            (t.get("role") or "").lower() == "presentation" for t in tables
        )
        nested = any(t.find("table") is not None for t in tables)
        if not (presentation or nested):
            return None
        return DesignSmell(
            self.code, "laid out with HTML tables", "medium",
            "page structure built from tables",
            "table-based layouts are a pre-2010 technique; they break on mobile "
            "and signal the site has not been touched in years",
        )


class InlineStyleHeavy(DesignSmellDetector):
    """A flood of inline style="" attributes => unmaintained, hand-patched."""
    code = "inline_style_heavy"

    def __init__(self, threshold: int = 15) -> None:
        self._threshold = threshold

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        n = len(ctx.soup.find_all(style=True))
        if n < self._threshold:
            return None
        return DesignSmell(
            self.code, "styling is patched inline", "low",
            f"{n} elements with inline style attributes",
            "heavy inline styling shows the site is hand-patched rather than "
            "designed, and is costly to keep consistent",
        )


class DatedFonts(DesignSmellDetector):
    """Comic Sans / Papyrus / default-only typography."""
    code = "dated_fonts"
    _PATTERNS = [
        (r"comic\s*sans", "Comic Sans"),
        (r"papyrus", "Papyrus"),
        (r"font-family\s*:\s*[\"']?times new roman", "Times New Roman as a body font"),
    ]

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        hit = None
        for pat, name in self._PATTERNS:
            if re.search(pat, ctx.html_lower):
                hit = name
                break
        if hit is None and ctx.soup.find("font", attrs={"face": True}):
            hit = "a <font face> declaration"
        if hit is None:
            return None
        return DesignSmell(
            self.code, "amateur typography", "medium",
            f"uses {hit}",
            "fonts like these read as unprofessional and undercut trust before "
            "a visitor reads a word",
        )


class OutdatedLibraries(DesignSmellDetector):
    """Decade-old front-end libraries referenced from the page."""
    code = "outdated_libs"
    _PATTERNS = [
        (r"jquery[.-/](?:js/)?1\.\d", "jQuery 1.x"),
        (r"jquery-1\.\d", "jQuery 1.x"),
        (r"bootstrap[./-](?:dist/)?[23]\.\d", "Bootstrap 2/3"),
        (r"bootstrap\.min\.(?:css|js)\?ver=[23]\.", "Bootstrap 2/3"),
    ]

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        for pat, name in self._PATTERNS:
            if re.search(pat, ctx.html_lower):
                return DesignSmell(
                    self.code, "built on outdated libraries", "medium",
                    f"loads {name}",
                    "a front-end stack this old means the site is years behind "
                    "on look, speed and security",
                )
        return None


class FixedPixelWidth(DesignSmellDetector):
    """A fixed pixel-width wrapper => a desktop-only, pre-responsive layout."""
    code = "fixed_width"

    def detect(self, ctx: SmellContext) -> Optional[DesignSmell]:
        # legacy width attributes on the main container, e.g. <table width="960">
        for el in ctx.soup.find_all(attrs={"width": True}):
            w = str(el.get("width", "")).strip().rstrip("px")
            if w.isdigit() and 600 <= int(w) <= 1100:
                return DesignSmell(
                    self.code, "fixed desktop-only width", "low",
                    f"a {w}px fixed-width container",
                    "a hard pixel width can't adapt to phones, so mobile "
                    "visitors get a tiny, zoomed-out page",
                )
        return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class DesignSmellScanner:
    """Runs every detector over the page and collects the smells."""

    def __init__(self, detectors: Sequence[DesignSmellDetector]) -> None:
        self._detectors = list(detectors)

    def scan(self, html: str) -> DesignSmellReport:
        if not html:
            return DesignSmellReport()
        ctx = SmellContext.build(html)
        smells = []
        for det in self._detectors:
            try:
                smell = det.detect(ctx)
            except Exception:
                # A flaky detector must never break the audit; skip it.
                smell = None
            if smell is not None:
                smells.append(smell)
        return DesignSmellReport(tuple(smells))


def build_default_scanner() -> DesignSmellScanner:
    """The standard set of heuristic design-smell detectors."""
    return DesignSmellScanner([
        NoViewportMeta(),
        DeprecatedTags(),
        LayoutTables(),
        InlineStyleHeavy(),
        DatedFonts(),
        OutdatedLibraries(),
        FixedPixelWidth(),
    ])
