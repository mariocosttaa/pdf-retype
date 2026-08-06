"""Finding a real font program for a family name.

A PDF usually embeds only a *subset* of each font — the handful of glyphs that
page needed. Writing new text with such a subset produces missing characters,
so when the embedded program falls short we go looking elsewhere: the machine's
installed fonts first, then a download from Fontsource (the Google Fonts and
open-source font mirror), and finally a base-14 stand-in.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc", ".pfb")

# Style words found in font names, mapped to CSS weights.
_WEIGHT_WORDS = [
    ("extrablack", 950), ("ultrablack", 950),
    ("extrabold", 800), ("ultrabold", 800),
    ("extralight", 200), ("ultralight", 200),
    ("semibold", 600), ("demibold", 600),
    ("semilight", 350),
    ("black", 900), ("heavy", 900),
    ("bold", 700),
    ("medium", 500),
    ("regular", 400), ("normal", 400),
    ("light", 300),
    ("thin", 100), ("hairline", 100),
]
# Deliberately absent: "roman" and "book" — they belong to family names far more
# often than they signal a weight (Times New Roman, Bookman).
_ITALIC_WORDS = ("italic", "oblique", "it")

# Foundry initials some producers glue onto the family name.
_FOUNDRY_SUFFIXES = ("mt", "ps", "psmt", "lt", "tt", "bt", "itc")

_CDN = "https://cdn.jsdelivr.net/fontsource/fonts"
_API = "https://api.fontsource.org/v1/fonts"
_USER_AGENT = "pdf-retype/0.1 (+https://github.com/)"


@dataclass(frozen=True)
class FontIdent:
    """A font family plus the weight/slant we want from it."""

    family: str
    weight: int = 400
    italic: bool = False

    @property
    def slug(self) -> str:
        """Fontsource id: ``Open Sans`` -> ``open-sans``."""
        return re.sub(r"[^a-z0-9]+", "-", self.family.lower()).strip("-")

    @property
    def style(self) -> str:
        return "italic" if self.italic else "normal"

    def __str__(self) -> str:
        bits = [self.family, str(self.weight)]
        if self.italic:
            bits.append("italic")
        return " ".join(bits)


def parse_font_name(basefont: str) -> FontIdent:
    """``AAAAAB+GTFlexa-Bold`` -> ``FontIdent('GT Flexa', 700, False)``."""
    name = basefont
    if len(name) > 7 and name[6] == "+" and name[:6].isalpha():
        name = name[7:]
    # Some producers repeat the name: "Arial Unicode MS+Arial Unicode MS"
    if "+" in name:
        head, _, tail = name.partition("+")
        name = head if head == tail else name.replace("+", " ")
    name = name.split(",")[0]

    tokens = [t for t in re.split(r"[-_\s]+", name) if t]
    # Split camelCase inside each token so "GTFlexaBold" separates too.
    parts: list[str] = []
    for token in tokens:
        parts.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", token) or [token])

    weight = 400
    italic = False
    family_parts: list[str] = []
    lowered = [p.lower() for p in parts]

    index = 0
    while index < len(parts):
        word = lowered[index]
        joined = "".join(lowered[index : index + 2])
        matched = False
        for candidate, value in _WEIGHT_WORDS:
            if joined == candidate:  # "Semi Bold" written as two tokens
                weight, matched = value, True
                index += 2
                break
            if word == candidate:
                weight, matched = value, True
                index += 1
                break
        if matched:
            continue
        if word in _ITALIC_WORDS:
            italic = True
            index += 1
            continue
        family_parts.append(parts[index])
        index += 1

    while len(family_parts) > 1 and family_parts[-1].lower() in _FOUNDRY_SUFFIXES:
        family_parts.pop()

    family = " ".join(family_parts).strip() or name
    return FontIdent(family, weight, italic)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def serif_from_program(buffer: bytes) -> bool | None:
    """Read serif-ness out of a font program's ``OS/2`` table.

    PDFs carry a serif bit in the font descriptor, but producers set it wrongly
    often enough that it cannot be trusted (TAP's booking PDF flags GT Flexa, a
    grotesque, as serif). The font file itself knows better: ``sFamilyClass``
    and the PANOSE bytes both classify the design. Returns None if unreadable.
    """
    try:
        if buffer[:4] == b"ttcf":  # collection: jump to the first face
            offset = int.from_bytes(buffer[12:16], "big")
        else:
            offset = 0
        table_count = int.from_bytes(buffer[offset + 4 : offset + 6], "big")
        directory = offset + 12
        for index in range(table_count):
            entry = directory + index * 16
            if buffer[entry : entry + 4] != b"OS/2":
                continue
            table = int.from_bytes(buffer[entry + 8 : entry + 12], "big")

            family_class = int.from_bytes(buffer[table + 30 : table + 32], "big") >> 8
            if family_class == 8:
                return False  # sans serif
            if 1 <= family_class <= 7:
                return True  # one of the serif classes

            serif_style = buffer[table + 32 + 1]  # PANOSE bSerifStyle
            if 2 <= serif_style <= 10 or serif_style == 14:
                return True
            if 11 <= serif_style <= 13 or serif_style == 15:
                return False
            return None
    except (IndexError, ValueError):
        return None
    return None


def cache_dir() -> Path:
    root = os.environ.get("PDF_RETYPE_CACHE") or os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    path = base / "pdf-retype" / "fonts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def system_font_dirs() -> list[Path]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        candidates = [
            home / "Library/Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Network/Library/Fonts"),
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            Path(local) / "Microsoft/Windows/Fonts" if local else None,
        ]
    else:
        share = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
        candidates = [
            home / ".fonts",
            Path(share) / "fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
        ]
    # The download cache is deliberately not listed here: its filenames carry a
    # unicode-range suffix that would confuse family matching. download_font()
    # checks it directly instead.
    return [c for c in candidates if c and c.is_dir()]


@lru_cache(maxsize=1)
def system_font_index() -> dict[str, list[Path]]:
    """Installed font files, grouped by normalized file stem."""
    index: dict[str, list[Path]] = {}
    for directory in system_font_dirs():
        for path in directory.rglob("*"):
            if path.suffix.lower() in FONT_EXTENSIONS and path.is_file():
                index.setdefault(_normalize(path.stem), []).append(path)
    return index


def _style_score(stem: str, family: str, ident: FontIdent) -> int:
    """How well a file stem matches the wanted weight and slant."""
    suffix = _normalize(stem)[len(_normalize(family)) :]
    parsed = parse_font_name(stem)
    score = 0
    score -= abs(parsed.weight - ident.weight) // 50
    if parsed.italic == ident.italic:
        score += 5
    else:
        score -= 8
    if not suffix:  # bare family name, likely the regular cut
        score += 2 if ident.weight == 400 and not ident.italic else 0
    if stem.lower().endswith(("[wght]", "variable", "vf")):
        score -= 1  # usable, but static cuts are safer
    return score


def find_system_font(ident: FontIdent) -> Path | None:
    """Best installed file for this family/weight/slant, if any."""
    wanted = _normalize(ident.family)
    if not wanted:
        return None

    matches: list[tuple[int, Path]] = []
    for key, paths in system_font_index().items():
        if not (key.startswith(wanted) or wanted.startswith(key)):
            continue
        # Guard against "Arial" matching "ArialRoundedBold" when a better cut exists.
        if len(key) > len(wanted) + 14:
            continue
        for path in paths:
            score = _style_score(path.stem, ident.family, ident)
            # Prefer the closest family name: "Arial Unicode MS" must not settle
            # for plain "Arial" when "Arial Unicode" sits right next to it.
            score -= abs(len(key) - len(wanted)) // 2
            if key == wanted:
                score += 3
            if path.suffix.lower() in (".ttc", ".otc"):
                score -= 4  # collections: we can only reach the first face
            matches.append((score, path))

    if not matches:
        return _fontconfig_match(ident)
    matches.sort(key=lambda item: (-item[0], -len(item[1].stem)))
    return matches[0][1]


def _fontconfig_match(ident: FontIdent) -> Path | None:
    """Ask fontconfig, when it is installed — it knows the real family names."""
    binary = shutil.which("fc-match")
    if not binary:
        return None
    pattern = f"{ident.family}:weight={ident.weight}"
    if ident.italic:
        pattern += ":slant=italic"
    try:
        out = subprocess.run(
            [binary, "-f", "%{file}\t%{family}", pattern],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except Exception:
        return None
    path_text, _, family = out.partition("\t")
    path = Path(path_text.strip())
    if not path.is_file() or path.suffix.lower() not in FONT_EXTENSIONS:
        return None
    # fc-match always answers something; reject an unrelated family.
    if _normalize(ident.family) not in _normalize(family):
        return None
    return path


def _http_get(url: str, timeout: float) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _fontsource_meta(slug: str, timeout: float) -> dict | None:
    raw = _http_get(f"{_API}/{slug}", timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _covers(data: bytes, text: str) -> int:
    """How many distinct characters of ``text`` this font program can draw."""
    import fitz  # local import: fontlib stays usable without a document open

    try:
        covered = set(fitz.Font(fontbuffer=data).valid_codepoints())
    except Exception:
        return -1
    wanted = {ch for ch in text if not ch.isspace()}
    return sum(1 for ch in wanted if ord(ch) in covered)


def download_font(
    ident: FontIdent, text: str = "", timeout: float = 15.0
) -> tuple[Path | None, str]:
    """Fetch a TTF from Fontsource into the cache. Returns (path, note).

    Fontsource splits each family by unicode range, so ``latin-ext`` holds *only*
    U+0100 and up — no ASCII at all. We therefore try the ranges in order and
    keep the first cut that covers every character we need to draw.
    """
    meta = _fontsource_meta(ident.slug, timeout)
    if meta is None:
        return None, f"{ident.family!r} not found on Fontsource (or no network)"

    weights = sorted(int(w) for w in meta.get("weights", []) or [400])
    styles = meta.get("styles") or ["normal"]
    subsets = list(meta.get("subsets") or ["latin"])

    style = ident.style if ident.style in styles else "normal"
    weight = min(weights, key=lambda w: abs(w - ident.weight)) if weights else 400

    # "latin" carries ASCII plus Latin-1 (accented vowels, ç, ñ) — the common case.
    preferred = [s for s in ("latin", "latin-ext") if s in subsets]
    ordered = preferred + [s for s in subsets if s not in preferred]

    needed = len({ch for ch in text if not ch.isspace()})
    best: tuple[int, Path, str] | None = None

    for subset in ordered:
        cached = cache_dir() / f"{ident.slug}-{subset}-{weight}-{style}.ttf"
        if cached.is_file():
            data = cached.read_bytes()
        else:
            data = _http_get(f"{_CDN}/{ident.slug}@latest/{subset}-{weight}-{style}.ttf", timeout)
            if not data:
                continue
            cached.write_bytes(data)

        note = f"downloaded {meta.get('family', ident.family)} {weight} {style} [{subset}]"
        if weight != ident.weight:
            note += f" (nearest cut to {ident.weight})"

        score = _covers(data, text)
        if not text or score >= needed:
            return cached, note
        if best is None or score > best[0]:
            best = (score, cached, note)

    if best is not None:
        return best[1], best[2] + " — partial character coverage"
    return None, f"Fontsource has {ident.family!r} but no usable TTF cut"


def load_font_bytes(
    ident: FontIdent, text: str = "", allow_download: bool = True, timeout: float = 15.0
) -> tuple[bytes | None, str]:
    """A font program for ``ident`` from disk or the network. Returns (bytes, note)."""
    local = find_system_font(ident)
    if local is not None:
        try:
            data = local.read_bytes()
        except OSError:
            data = None
        if data and (not text or _covers(data, text) >= len({c for c in text if not c.isspace()})):
            return data, f"system font {local.name}"

    if not allow_download:
        return None, f"no local font for {ident.family!r} (downloads disabled)"

    path, note = download_font(ident, text, timeout)
    if path is not None:
        try:
            return path.read_bytes(), note
        except OSError as exc:
            return None, f"cached font unreadable: {exc}"

    if local is not None:  # nothing better online; the local file is still closer
        try:
            return local.read_bytes(), f"system font {local.name} — partial coverage"
        except OSError:
            pass
    return None, note
