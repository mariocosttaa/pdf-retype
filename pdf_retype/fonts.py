"""Font inspection and resolution.

Two jobs live here:

1. Reporting which fonts a PDF actually uses (``pdf-retype fonts``).
2. Turning a text span into something we can *write back* with. That is the hard
   part: a PDF names a font like ``ABCDEF+Helvetica-Bold``, but to draw new text
   we need either the embedded font program itself or a sane base-14 stand-in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from pdf_retype.fontlib import load_font_bytes, parse_font_name, serif_from_program

# Text span flag bits, as documented by PyMuPDF.
FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC = 1 << 1
FLAG_SERIF = 1 << 2
FLAG_MONOSPACE = 1 << 3
FLAG_BOLD = 1 << 4

# base-14 families, indexed by (bold, italic).
_BASE14 = {
    "helv": {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"},
    "tiro": {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"},
    "cour": {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"},
}

# Font programs we know how to hand back to MuPDF.
_USABLE_EXT = {"ttf", "otf", "cff", "pfa", "pfb", "ttc"}

# Families every PDF reader already has — no point embedding these.
_BASE14_FAMILIES = {
    "helvetica", "arial", "arialmt", "times", "timesnewroman", "timesroman",
    "courier", "couriernew", "symbol", "zapfdingbats",
}


def _normalize_family(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# Whitespace is drawn by advancing the pen, not by a glyph.
_ALWAYS_DRAWABLE = set(" \t\n\r ")


def missing_in_font(font: fitz.Font, text: str) -> list[str]:
    """Characters of ``text`` that ``font`` has no glyph for."""
    try:
        covered = set(font.valid_codepoints())
    except Exception:
        return list(dict.fromkeys(text))
    return [
        ch for ch in dict.fromkeys(text)
        if ch not in _ALWAYS_DRAWABLE and ord(ch) not in covered
    ]


@dataclass
class FontUse:
    """A font as referenced by a page's resource dictionary."""

    xref: int
    name: str
    basefont: str
    type: str
    encoding: str
    embedded: bool
    pages: list[int] = field(default_factory=list)

    @property
    def family(self) -> str:
        """``ABCDEF+Helvetica-Bold`` -> ``Helvetica-Bold``."""
        return strip_subset_tag(self.basefont)

    def as_dict(self) -> dict:
        return {
            "xref": self.xref,
            "name": self.name,
            "basefont": self.basefont,
            "family": self.family,
            "type": self.type,
            "encoding": self.encoding,
            "embedded": self.embedded,
            "pages": [p + 1 for p in self.pages],
        }


@dataclass
class FontSpec:
    """A font we can actually draw with."""

    fontname: str  # name registered in the page resources (a hash, for buffers)
    fontbuffer: bytes | None  # font program, when we carry one
    source: str  # embedded | system | downloaded | base14 | forced
    origin_font: str  # what the PDF called it
    note: str = ""  # human-readable provenance
    display: str = ""  # family name to show the user

    def __post_init__(self) -> None:
        self._font: fitz.Font | None = None

    @property
    def name(self) -> str:
        """The name worth showing: a hashed resource id helps nobody."""
        return self.display or self.fontname

    @property
    def label(self) -> str:
        return f"{self.name} ({self.note or self.source})"

    def font(self) -> fitz.Font:
        if self._font is None:
            if self.fontbuffer is not None:
                self._font = fitz.Font(fontbuffer=self.fontbuffer)
            else:
                self._font = fitz.Font(fontname=self.fontname)
        return self._font

    def usable(self) -> bool:
        try:
            self.font()
        except Exception:
            return False
        return True

    def has_charmap(self) -> bool:
        """Whether the program maps characters to glyphs on its own.

        CID-keyed fonts (``Identity-H``) usually ship without a ``cmap`` table:
        the PDF holds the mapping instead. Such a program is fine for displaying
        the existing page but useless for setting new text.
        """
        try:
            return len(self.font().valid_codepoints()) > 0
        except Exception:
            return False

    def text_length(self, text: str, fontsize: float) -> float:
        """Width of ``text`` in points at ``fontsize``."""
        return self.font().text_length(text, fontsize)

    def missing_glyphs(self, text: str) -> list[str]:
        """Characters this font cannot draw (embedded subsets are often partial).

        ``valid_codepoints()`` is the reliable check here: ``has_glyph`` returns a
        glyph id whose zero value does not actually mean "absent", and
        ``glyph_advance`` answers from a fallback font.
        """
        try:
            font = self.font()
        except Exception:
            return list(dict.fromkeys(text))
        return missing_in_font(font, text)


def strip_subset_tag(basefont: str) -> str:
    """Drop the six-letter subset prefix PDF producers prepend."""
    if len(basefont) > 7 and basefont[6] == "+" and basefont[:6].isalpha():
        return basefont[7:]
    return basefont


def list_fonts(doc: fitz.Document, pages: list[int] | None = None) -> list[FontUse]:
    """Every font referenced by the given pages (all pages when ``pages`` is None)."""
    targets = range(doc.page_count) if pages is None else pages
    found: dict[int, FontUse] = {}
    for pno in targets:
        for xref, ext, ftype, basefont, name, encoding in doc.get_page_fonts(pno):
            entry = found.get(xref)
            if entry is None:
                entry = FontUse(
                    xref=xref,
                    name=name,
                    basefont=basefont,
                    type=ftype,
                    encoding=encoding or "",
                    embedded=ext not in ("n/a", ""),
                )
                found[xref] = entry
            entry.pages.append(pno)
    return sorted(found.values(), key=lambda f: (f.family.lower(), f.xref))


def spans_in_rect(page: fitz.Page, rect: fitz.Rect) -> list[dict]:
    """Text spans overlapping ``rect``, richest overlap first."""
    hits = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                overlap = fitz.Rect(span["bbox"]) & rect
                if not overlap.is_empty:
                    hits.append((overlap.get_area(), span))
    hits.sort(key=lambda item: item[0], reverse=True)
    return [span for _, span in hits]


def describe_span(span: dict) -> dict:
    """Font attributes of a span, in plain values."""
    flags = span["flags"]
    return {
        "font": strip_subset_tag(span["font"]),
        "raw_font": span["font"],
        "size": round(span["size"], 2),
        "color": fitz.sRGB_to_pdf(span["color"]),
        "color_hex": f"#{span['color']:06x}",
        "bold": bool(flags & FLAG_BOLD) or "bold" in span["font"].lower(),
        "italic": bool(flags & FLAG_ITALIC)
        or any(t in span["font"].lower() for t in ("italic", "oblique")),
        "serif": bool(flags & FLAG_SERIF),
        "monospace": bool(flags & FLAG_MONOSPACE),
    }


_SERIF_NAME_HINTS = (
    "times", "serif", "georgia", "garamond", "roman", "book", "minion",
    "caslon", "baskerville", "cambria", "palatino", "merriweather", "playfair",
)
_SANS_NAME_HINTS = (
    "sans", "grotesk", "grotesque", "gothic", "helvetica", "arial", "verdana",
    "tahoma", "calibri", "inter", "roboto", "futura", "avenir", "geist", "neue",
)


def base14_for(style: dict) -> str:
    """Pick the closest base-14 font for a span description.

    Note what is *not* consulted: the PDF's own serif flag. Producers set it
    wrongly often enough to be useless — TAP's booking PDF flags GT Flexa, a
    grotesque sans, as serif. We trust the font program's OS/2 table when we have
    it (``serif_program``), then the family name, and otherwise default to sans,
    which is the overwhelming norm in digital documents. ``--font`` overrides.
    """
    name = style["raw_font"].lower()
    evidence = style.get("serif_program")

    if style["monospace"] or any(t in name for t in ("courier", "mono")):
        family = "cour"
    elif any(t in name for t in _SERIF_NAME_HINTS):
        family = "tiro"
    elif any(t in name for t in _SANS_NAME_HINTS):
        family = "helv"
    elif evidence is True:
        family = "tiro"
    else:
        family = "helv"
    return _BASE14[family][(int(style["bold"]), int(style["italic"]))]


def extract_embedded(doc: fitz.Document, page: fitz.Page, raw_font: str) -> bytes | None:
    """The embedded font program behind ``raw_font`` on this page, if reusable.

    Names are compared by identity rather than literally: a span reports
    ``Arial Unicode MS`` while the resource dictionary may call the very same
    font ``Arial Unicode MS+Arial Unicode MS``.
    """
    wanted = parse_font_name(raw_font)
    for xref, ext, _ftype, basefont, _name, _enc in doc.get_page_fonts(page.number):
        if basefont != raw_font and parse_font_name(basefont) != wanted:
            continue
        if ext.lower() not in _USABLE_EXT:
            return None
        try:
            info = doc.extract_font(xref)
        except Exception:
            return None
        buffer = info[3] if isinstance(info, (tuple, list)) else info.get("content")
        if not buffer:
            return None
        try:  # make sure MuPDF can parse it back
            fitz.Font(fontbuffer=buffer)
        except Exception:
            return None
        return bytes(buffer)
    return None


def _buffer_spec(
    buffer: bytes, source: str, origin: str, note: str, display: str = ""
) -> FontSpec:
    tag = hashlib.sha1(buffer).hexdigest()[:10]
    return FontSpec(f"F{tag}", buffer, source, origin, note, display)


def resolve_font(
    doc: fitz.Document,
    page: fitz.Page,
    style: dict,
    new_text: str,
    forced: str | None = None,
    font_file: str | None = None,
    prefer_embedded: bool = True,
    allow_download: bool = True,
    timeout: float = 15.0,
) -> tuple[FontSpec, list[str]]:
    """Choose a font able to draw ``new_text`` in the style of the matched span.

    The cascade, in order of fidelity:

    1. whatever the caller forced;
    2. the font embedded in the PDF — perfect match, but usually a subset;
    3. a base-14 face, when the family *is* one of the base-14 (no need to embed);
    4. the same family installed on this machine, or downloaded from Fontsource;
    5. a base-14 stand-in, as a last resort.

    Returns the spec plus any warnings worth showing the user.
    """
    origin = style["raw_font"]
    warnings: list[str] = []
    style = dict(style)

    if font_file:
        try:
            buffer = Path(font_file).read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read font file {font_file}: {exc}") from exc
        spec = _buffer_spec(buffer, "forced", origin, f"file {Path(font_file).name}",
                            display=Path(font_file).stem)
        missing = spec.missing_glyphs(new_text)
        if missing:
            warnings.append(f"{Path(font_file).name} cannot draw {''.join(missing)!r}")
        return spec, warnings

    if forced:
        return FontSpec(forced, None, "forced", origin, f"forced {forced}"), warnings

    ident = parse_font_name(origin)

    if prefer_embedded:
        buffer = extract_embedded(doc, page, origin)
        if buffer:
            # Even a subset we cannot draw with may still classify the design.
            style.setdefault("serif_program", serif_from_program(buffer))
            spec = _buffer_spec(buffer, "embedded", origin, "embedded in the PDF",
                                display=style["font"])
            missing = spec.missing_glyphs(new_text)
            if not missing:
                return spec, warnings
            if not spec.has_charmap():
                warnings.append(
                    f"the embedded {style['font']} is CID-keyed and carries no character"
                    " map — looking for the full family"
                )
            else:
                warnings.append(
                    f"the embedded {style['font']} is a subset without {''.join(missing)!r}"
                    " — looking for the full family"
                )

    # A base-14 family needs no embedding at all, and every reader has it.
    if _normalize_family(ident.family) in _BASE14_FAMILIES:
        spec = FontSpec(base14_for(style), None, "base14", origin, "base-14 standard font")
        if not spec.missing_glyphs(new_text):
            return spec, warnings

    buffer, note = load_font_bytes(
        ident, new_text, allow_download=allow_download, timeout=timeout
    )
    if buffer:
        if style.get("serif_program") is None:
            style["serif_program"] = serif_from_program(buffer)
        source = "downloaded" if note.startswith(("downloaded", "cache")) else "system"
        spec = _buffer_spec(buffer, source, origin, note, display=str(ident))
        missing = spec.missing_glyphs(new_text)
        if spec.usable() and not missing:
            return spec, warnings
        if missing:
            warnings.append(f"{note} still cannot draw {''.join(missing)!r}")
    else:
        warnings.append(note)

    fallback = FontSpec(base14_for(style), None, "base14", origin, "base-14 stand-in")
    missing = fallback.missing_glyphs(new_text)
    warnings.append(
        f"drawing with {fallback.fontname} instead of {style['font']}"
        " — the shape will differ from the original"
    )
    if missing:
        warnings.append(f"characters {''.join(missing)!r} cannot be drawn at all")
    return fallback, warnings
