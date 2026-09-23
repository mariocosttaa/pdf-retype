"""Rewriting matched text in place.

The strategy is: redact only the *text* of a match (leaving images and vector
art underneath untouched), then draw the replacement on the original baseline
with the font we resolved from the span it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fitz

from pdf_retype.fonts import FontSpec, resolve_font
from pdf_retype.search import Hit

ALIGNMENTS = ("left", "center", "right")
FIT_MODES = ("auto", "shrink", "overflow")

_ABSOLUTE_MIN_SIZE = 3.0
_PAGE_MARGIN = 36.0  # half an inch, when nothing else bounds the line
_NEIGHBOUR_GAP = 2.0  # breathing room before the next word on the line


@dataclass
class Change:
    """What happened to a single match."""

    page: int
    old_text: str
    new_text: str
    score: float
    font: str
    font_source: str
    fontsize: float
    original_fontsize: float
    font_note: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "page": self.page + 1,
            "old": self.old_text,
            "new": self.new_text,
            "score": round(self.score, 3),
            "font": self.font,
            "font_source": self.font_source,
            "font_note": self.font_note,
            "fontsize": round(self.fontsize, 2),
            "original_fontsize": round(self.original_fontsize, 2),
            "warnings": self.warnings,
        }


def parse_color(value: str) -> tuple[float, float, float]:
    """``#rrggbb`` / ``rrggbb`` / ``r,g,b`` (0-255 or 0-1) -> PDF float triple."""
    text = value.strip().lstrip("#")
    if "," in text:
        parts = [float(p) for p in text.split(",")]
        if len(parts) != 3:
            raise ValueError(f"expected three components, got {value!r}")
        if any(p > 1 for p in parts):
            parts = [p / 255 for p in parts]
        return tuple(min(1.0, max(0.0, p)) for p in parts)
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a colour: {value!r}")
    return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))


def redaction_rect(page: fitz.Page, hit: Hit) -> fitz.Rect:
    """The area to clear, trimmed so it cannot bite into neighbouring lines.

    A search rectangle spans the full line height, which routinely overlaps the
    line above or below by a fraction of a point. Redaction deletes every glyph
    it touches, so an untrimmed rectangle silently erases unrelated text.
    """
    rect = fitz.Rect(hit.rect)

    for word in page.get_text("words"):
        word_rect = fitz.Rect(word[:4])
        overlap = word_rect & rect
        if overlap.is_empty or not word[4].strip():
            continue
        # Anything sharing most of our height sits on our own line.
        if overlap.height > 0.6 * rect.height:
            continue
        if word_rect.y0 >= rect.y0 + rect.height * 0.5:
            rect.y1 = min(rect.y1, word_rect.y0 - 0.05)  # neighbour below
        elif word_rect.y1 <= rect.y1 - rect.height * 0.5:
            rect.y0 = max(rect.y0, word_rect.y1 + 0.05)  # neighbour above

    return rect


def free_width(page: fitz.Page, rect: fitz.Rect) -> float:
    """How far the text may run before it would collide with something.

    Longer replacements are the common case ("Ana" -> "Ana Maria"), and shrinking
    them when the line has room to spare looks worse than simply using that room.
    We stop at the nearest text to the right on the same line, or at the page
    margin when the line is clear.

    This walks words rather than spans on purpose: a match often sits in the
    middle of a long span, whose own left edge says nothing about what follows
    the match.
    """
    band_top = rect.y0 + rect.height * 0.25
    band_bottom = rect.y1 - rect.height * 0.25
    limit = page.rect.x1 - _PAGE_MARGIN

    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if x0 < rect.x1 - 0.5:  # not to our right
            continue
        if y1 <= band_top or y0 >= band_bottom:  # not on our line
            continue
        if not text.strip():
            continue
        limit = min(limit, x0 - _NEIGHBOUR_GAP)

    return max(rect.width, limit - rect.x0)


def _fit_size(spec: FontSpec, text: str, size: float, box: float, room: float,
              mode: str, min_ratio: float) -> tuple[float, list[str]]:
    """Decide the font size for ``text``.

    ``box`` is the width the old word occupied; ``room`` is how far the line can
    run before hitting something. ``auto`` spends the free room before shrinking,
    ``shrink`` stays inside the old box, ``overflow`` never resizes.
    """
    warnings: list[str] = []
    if not text:
        return size, warnings

    needed = spec.text_length(text, size)
    limit = room if mode == "auto" else box

    if needed <= limit:
        if needed > box + 0.5:
            warnings.append(f"{needed - box:.0f}pt wider than the original, into free space")
        return size, warnings

    if mode == "overflow":
        warnings.append(
            f"{needed - box:.1f}pt wider than the original and may overlap what follows"
        )
        return size, warnings

    floor = max(_ABSOLUTE_MIN_SIZE, size * min_ratio)
    scaled = size * limit / needed
    if scaled < floor:
        warnings.append(
            f"does not fit even at {floor:.1f}pt (the floor set by --min-ratio); it will overflow"
        )
        return floor, warnings
    if round(scaled, 1) < round(size, 1):  # a shrink too small to see is not news
        warnings.append(f"shrunk from {size:.1f}pt to {scaled:.1f}pt to fit")
    return scaled, warnings


_ROUNDTRIP_CACHE: dict[tuple[str, str], str] = {}


def _extracted(spec: FontSpec, text: str) -> str:
    """``text`` drawn with ``spec``, then read back out of the PDF.

    Cheaper to find out by drawing it once on a throwaway page than to guess
    from the font tables.
    """
    key = (spec.fontname, text)
    if key not in _ROUNDTRIP_CACHE:
        probe = fitz.open()
        page = probe.new_page()
        writer = fitz.TextWriter(page.rect)
        writer.append(fitz.Point(20, 40), text, font=spec.font(), fontsize=12)
        writer.write_text(page)
        _ROUNDTRIP_CACHE[key] = page.get_text().strip()
        probe.close()
    return _ROUNDTRIP_CACHE[key]


def writes_cleanly(spec: FontSpec, text: str) -> bool:
    """Whether drawing ``text`` extracts back as the same characters.

    Fonts routinely point several codepoints at one glyph — U+0020 and U+00A0
    share a space, U+002D and U+00AD share a hyphen — and MuPDF records whichever
    one it meets first in the reverse mapping. The replacement then looks perfect
    on the page but comes back out of the PDF as ``Mariana\\xa0Pereira`` or
    ``23\\xad09\\xad2026``, which no longer matches a search for what was typed.
    """
    return _extracted(spec, text) == text.strip()


def roundtrip_warning(spec: FontSpec, text: str) -> str | None:
    """What to tell the user when the text will not extract as they typed it.

    Only worth saying once the word-by-word rescue in ``_append_text`` has had
    its chance: that fixes an ambiguous *space*, but nothing can be split around
    a hyphen sitting inside a word.
    """
    if not text or writes_cleanly(spec, text):
        return None
    words = [word for word in text.split(" ") if word]
    if [_extracted(spec, word) for word in words] == words:
        return None
    got = " ".join(_extracted(spec, word) for word in words)
    return (
        f"{spec.name} points several characters at one glyph: the text will look"
        f" right but extracts as {got!r}, so a search for it will not match"
    )


def _append_text(writer: fitz.TextWriter, spec: FontSpec, point: fitz.Point,
                 text: str, size: float) -> None:
    """Add ``text`` to the writer, word by word when a single run would not
    survive extraction."""
    if writes_cleanly(spec, text):
        writer.append(point, text, font=spec.font(), fontsize=size)
        return

    # Placing each word separately lets the extractor infer the space from the
    # gap, which round-trips correctly whatever the font's glyph mapping does.
    x = point.x
    for word in text.split(" "):
        if word:
            writer.append(fitz.Point(x, point.y), word, font=spec.font(), fontsize=size)
        x += spec.text_length(word + " ", size)


def _draw_x(rect: fitz.Rect, width: float, align: str) -> float:
    if align == "center":
        return rect.x0 + (rect.width - width) / 2
    if align == "right":
        return rect.x1 - width
    return rect.x0


def plan_hits(
    doc: fitz.Document,
    hits: list[Hit],
    new_text: str,
    fit: str = "auto",
    min_ratio: float = 0.6,
    font: str | None = None,
    font_file: str | None = None,
    fontsize: float | None = None,
    prefer_embedded: bool = True,
    allow_download: bool = True,
    timeout: float = 15.0,
) -> list[tuple[Hit, object, float, list[str]]]:
    """Work out font and size for each hit without touching the document."""
    plans = []
    for hit in hits:
        page = doc[hit.page]
        spec, warnings = resolve_font(
            doc, page, hit.style, new_text,
            forced=font, font_file=font_file,
            prefer_embedded=prefer_embedded,
            allow_download=allow_download, timeout=timeout,
        )
        size = fontsize or hit.style["size"]
        room = free_width(page, hit.rect) if fit == "auto" else hit.rect.width
        size, fit_warnings = _fit_size(
            spec, new_text, size, hit.rect.width, room, fit, min_ratio
        )
        warnings = warnings + fit_warnings
        damage = roundtrip_warning(spec, new_text)
        if damage:
            warnings.append(damage)
        plans.append((hit, spec, size, warnings))
    return plans


def describe_plan(hit: Hit, spec, size: float, warnings: list[str], new_text: str) -> Change:
    return Change(
        page=hit.page,
        old_text=hit.text,
        new_text=new_text,
        score=hit.score,
        font=spec.name,
        font_source=spec.source,
        font_note=spec.note,
        fontsize=size,
        original_fontsize=hit.style["size"],
        warnings=warnings,
    )


def replace_hits(
    doc: fitz.Document,
    hits: list[Hit],
    new_text: str,
    align: str = "left",
    fit: str = "auto",
    min_ratio: float = 0.6,
    color: tuple[float, float, float] | None = None,
    fill: tuple[float, float, float] | None = None,
    font: str | None = None,
    font_file: str | None = None,
    fontsize: float | None = None,
    prefer_embedded: bool = True,
    allow_download: bool = True,
    timeout: float = 15.0,
) -> list[Change]:
    """Replace every hit with ``new_text``. Mutates ``doc`` in memory."""
    if align not in ALIGNMENTS:
        raise ValueError(f"align must be one of {ALIGNMENTS}")
    if fit not in FIT_MODES:
        raise ValueError(f"fit must be one of {FIT_MODES}")

    plans = plan_hits(
        doc, hits, new_text, fit=fit, min_ratio=min_ratio, font=font,
        font_file=font_file, fontsize=fontsize, prefer_embedded=prefer_embedded,
        allow_download=allow_download, timeout=timeout,
    )

    by_page: dict[int, list] = {}
    for plan in plans:
        by_page.setdefault(plan[0].page, []).append(plan)

    changes: list[Change] = []

    for pno, page_plans in sorted(by_page.items()):
        page = doc[pno]

        for hit, _spec, _size, _warnings in page_plans:
            page.add_redact_annot(redaction_rect(page, hit),
                                  fill=fill if fill is not None else False,
                                  cross_out=False)

        # Take out the text only: images and vector art underneath stay put.
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )

        for hit, spec, size, warnings in page_plans:
            if new_text:
                width = spec.text_length(new_text, size)
                point = fitz.Point(_draw_x(hit.rect, width, align), hit.origin[1])
                writer = fitz.TextWriter(page.rect)
                _append_text(writer, spec, point, new_text, size)
                writer.write_text(page, color=color if color is not None else hit.style["color"])
            changes.append(describe_plan(hit, spec, size, warnings, new_text))

    return changes
