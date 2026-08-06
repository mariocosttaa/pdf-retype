"""Searching a PDF for a word — exactly, or for something close enough."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

import fitz

from pdf_retype.fonts import describe_span, spans_in_rect

_PUNCT_STRIP = ".,;:!?()[]{}\"'`«»“”‘’–—-…/\\"


@dataclass
class Hit:
    """One occurrence of the search term on a page."""

    page: int  # 0-based
    rect: fitz.Rect
    text: str  # what is actually printed there
    score: float  # 1.0 for an exact match
    style: dict  # font attributes of the underlying span
    origin: tuple[float, float]  # baseline start of the span

    def as_dict(self) -> dict:
        return {
            "page": self.page + 1,
            "text": self.text,
            "score": round(self.score, 3),
            "rect": [round(v, 2) for v in self.rect],
            "font": self.style["font"],
            "size": self.style["size"],
            "color": self.style["color_hex"],
            "bold": self.style["bold"],
            "italic": self.style["italic"],
        }


def normalize(text: str, ignore_accents: bool = True) -> str:
    """Casefold, trim punctuation and (optionally) strip accents for comparison."""
    text = text.strip(_PUNCT_STRIP).casefold()
    if ignore_accents:
        decomposed = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return text


def similarity(a: str, b: str, ignore_accents: bool = True) -> float:
    return SequenceMatcher(None, normalize(a, ignore_accents), normalize(b, ignore_accents)).ratio()


def _style_for(page: fitz.Page, rect: fitz.Rect) -> tuple[dict, tuple[float, float]]:
    spans = spans_in_rect(page, rect)
    if not spans:
        return (
            {
                "font": "unknown",
                "raw_font": "unknown",
                "size": round(rect.height * 0.8, 2),
                "color": (0.0, 0.0, 0.0),
                "color_hex": "#000000",
                "bold": False,
                "italic": False,
                "serif": False,
                "monospace": False,
            },
            (rect.x0, rect.y1),
        )
    span = spans[0]
    return describe_span(span), tuple(span["origin"])


def _printed_text(page: fitz.Page, rect: fitz.Rect, term: str) -> str:
    """The text as printed at ``rect``.

    A search rectangle spans the whole line height and can clip a sliver of the
    line below, so the extraction may come back with more than one line. Keep the
    one that actually corresponds to the term.
    """
    extracted = page.get_textbox(rect).strip()
    if not extracted:
        return term
    lines = [line.strip() for line in extracted.splitlines() if line.strip()]
    if len(lines) <= 1:
        return lines[0] if lines else term
    return max(lines, key=lambda line: similarity(line, term))


def _pages(doc: fitz.Document, pages: list[int] | None):
    return range(doc.page_count) if pages is None else pages


def find_exact(
    doc: fitz.Document,
    term: str,
    pages: list[int] | None = None,
    case_sensitive: bool = False,
) -> list[Hit]:
    """Literal occurrences of ``term``."""
    hits: list[Hit] = []
    for pno in _pages(doc, pages):
        page = doc[pno]
        for rect in page.search_for(term):
            printed = _printed_text(page, rect, term)
            if case_sensitive and term not in printed:
                continue
            style, origin = _style_for(page, rect)
            hits.append(Hit(pno, rect, printed, 1.0, style, origin))
    return hits


def find_similar(
    doc: fitz.Document,
    term: str,
    threshold: float = 0.82,
    pages: list[int] | None = None,
    ignore_accents: bool = True,
) -> list[Hit]:
    """Occurrences of anything close to ``term``, scored by similarity.

    Words are grouped into n-grams the same length as the term, and only within
    a single line, so every hit maps to one contiguous rectangle.
    """
    span_len = max(1, len(term.split()))
    hits: list[Hit] = []

    for pno in _pages(doc, pages):
        page = doc[pno]
        lines: dict[tuple[int, int], list[tuple]] = {}
        for word in page.get_text("words"):
            lines.setdefault((word[5], word[6]), []).append(word)

        for words in lines.values():
            words.sort(key=lambda w: w[7])
            for start in range(len(words) - span_len + 1):
                group = words[start : start + span_len]
                candidate = " ".join(w[4] for w in group)
                score = similarity(candidate, term, ignore_accents)
                if score < threshold:
                    continue
                rect = fitz.Rect(group[0][:4])
                for word in group[1:]:
                    rect |= fitz.Rect(word[:4])
                style, origin = _style_for(page, rect)
                hits.append(Hit(pno, rect, candidate, score, style, origin))

    return _dedupe(hits)


def _dedupe(hits: list[Hit]) -> list[Hit]:
    """Overlapping n-grams describe the same spot; keep the best-scoring one."""
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (-h.score, h.page, h.rect.y0, h.rect.x0)):
        clash = False
        for other in kept:
            if other.page != hit.page:
                continue
            overlap = other.rect & hit.rect
            if not overlap.is_empty and overlap.get_area() > 0.3 * min(
                hit.rect.get_area(), other.rect.get_area()
            ):
                clash = True
                break
        if not clash:
            kept.append(hit)
    return sorted(kept, key=lambda h: (h.page, h.rect.y0, h.rect.x0))


def find(
    doc: fitz.Document,
    term: str,
    fuzzy: bool = False,
    threshold: float = 0.82,
    pages: list[int] | None = None,
    case_sensitive: bool = False,
    ignore_accents: bool = True,
) -> list[Hit]:
    if fuzzy:
        return find_similar(doc, term, threshold, pages, ignore_accents)
    return find_exact(doc, term, pages, case_sensitive)
