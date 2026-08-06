"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import fitz

from pdf_retype import __version__
from pdf_retype.fontlib import cache_dir, parse_font_name
from pdf_retype.fonts import list_fonts, resolve_font, strip_subset_tag
from pdf_retype.replace import (
    ALIGNMENTS,
    FIT_MODES,
    parse_color,
    plan_hits,
    describe_plan,
    replace_hits,
)
from pdf_retype.search import find

OK = "ok"
WARN = "!"


def parse_pages(spec: str | None, page_count: int) -> list[int] | None:
    """``1,3,5-7`` -> zero-based page indices. ``None`` means every page."""
    if not spec:
        return None
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            start_text, _, end_text = chunk.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(chunk)
        if start < 1 or end > page_count or start > end:
            raise ValueError(f"page range {chunk!r} is outside 1-{page_count}")
        pages.extend(range(start - 1, end))
    return sorted(set(pages))


def open_pdf(path: str) -> fitz.Document:
    source = Path(path)
    if not source.is_file():
        raise SystemExit(f"error: no such file: {path}")
    try:
        doc = fitz.open(source)
    except Exception as exc:
        raise SystemExit(f"error: cannot open {path}: {exc}") from exc
    if doc.needs_pass:
        raise SystemExit(f"error: {path} is password protected")
    if not doc.is_pdf:
        raise SystemExit(f"error: {path} is not a PDF")
    return doc


def style_from_basefont(basefont: str) -> dict:
    """A span-like description built from a font name alone."""
    ident = parse_font_name(basefont)
    lowered = basefont.lower()
    return {
        "font": strip_subset_tag(basefont),
        "raw_font": basefont,
        "size": 10.0,
        "color": (0.0, 0.0, 0.0),
        "color_hex": "#000000",
        "bold": ident.weight >= 600,
        "italic": ident.italic,
        "serif": any(t in lowered for t in ("times", "serif", "georgia", "garamond")),
        "monospace": any(t in lowered for t in ("courier", "mono")),
    }


# --------------------------------------------------------------------------- fonts


def cmd_fonts(args: argparse.Namespace) -> int:
    doc = open_pdf(args.pdf)
    pages = parse_pages(args.pages, doc.page_count)
    fonts = list_fonts(doc, pages)

    if not fonts:
        print("No fonts found (the pages may hold only images or vector art).")
        return 0

    rows = []
    for use in fonts:
        row = use.as_dict()
        if args.plan:
            page = doc[use.pages[0]]
            spec, warnings = resolve_font(
                doc, page, style_from_basefont(use.basefont), args.plan,
                allow_download=not args.offline,
            )
            row["plan"] = {
                "source": spec.source,
                "note": spec.note,
                "draws_with": spec.name,
                "warnings": warnings,
            }
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f"{Path(args.pdf).name}: {len(fonts)} font(s) across {doc.page_count} page(s)\n")
    for row in rows:
        embedded = "embedded" if row["embedded"] else "not embedded"
        print(f"  {row['family']}")
        print(
            f"    type {row['type']}  |  {embedded}  |  encoding {row['encoding'] or '-'}"
            f"  |  pages {_compact(row['pages'])}  |  xref {row['xref']}"
        )
        plan = row.get("plan")
        if plan:
            print(f"    -> replacement text would use: {plan['note'] or plan['source']}")
            for warning in plan["warnings"]:
                print(f"       {WARN} {warning}")
        print()
    return 0


def _compact(pages: list[int]) -> str:
    if len(pages) > 6:
        return f"{pages[0]}-{pages[-1]} ({len(pages)})"
    return ",".join(str(p) for p in pages)


# ---------------------------------------------------------------------------- find


def cmd_find(args: argparse.Namespace) -> int:
    doc = open_pdf(args.pdf)
    pages = parse_pages(args.pages, doc.page_count)
    hits = find(
        doc, args.term,
        fuzzy=args.fuzzy, threshold=args.threshold, pages=pages,
        case_sensitive=args.case_sensitive, ignore_accents=not args.strict_accents,
    )

    if args.json:
        print(json.dumps([h.as_dict() for h in hits], indent=2, ensure_ascii=False))
        return 0 if hits else 1

    if not hits:
        how = f"similar to {args.term!r}" if args.fuzzy else f"{args.term!r}"
        print(f"No match for {how}.")
        if not args.fuzzy:
            print("Try --fuzzy to catch near matches (different accents, spelling, case).")
        return 1

    kind = "similar match" if args.fuzzy else "match"
    label = kind if len(hits) == 1 else kind + "es"
    print(f"{len(hits)} {label} in {Path(args.pdf).name}\n")
    for hit in hits:
        style = hit.style
        traits = [t for t, on in (("bold", style["bold"]), ("italic", style["italic"])) if on]
        score = "" if hit.score >= 0.999 else f"  score {hit.score:.2f}"
        print(f"  p.{hit.page + 1}  {hit.text!r}{score}")
        print(
            f"       {style['font']} {style['size']}pt"
            f"{' ' + '+'.join(traits) if traits else ''}  {style['color_hex']}"
            f"  at ({hit.rect.x0:.0f}, {hit.rect.y0:.0f})"
        )
    return 0


# ------------------------------------------------------------------------- replace


def cmd_replace(args: argparse.Namespace) -> int:
    doc = open_pdf(args.pdf)
    source = Path(args.pdf)
    pages = parse_pages(args.pages, doc.page_count)

    try:
        color = parse_color(args.color) if args.color else None
        fill = parse_color(args.fill) if args.fill else None
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    hits = find(
        doc, args.target,
        fuzzy=args.fuzzy, threshold=args.threshold, pages=pages,
        case_sensitive=args.case_sensitive, ignore_accents=not args.strict_accents,
    )
    if not hits:
        print(f"No match for {args.target!r} — nothing to replace.")
        if not args.fuzzy:
            print("Try --fuzzy to catch near matches.")
        return 1

    if args.limit:
        dropped = len(hits) - args.limit
        hits = hits[: args.limit]
        if dropped > 0:
            print(f"note: --limit {args.limit} keeps the first {args.limit} of "
                  f"{args.limit + dropped} matches\n")

    if args.dry_run:
        plans = plan_hits(
            doc, hits, args.replacement, fit=args.fit, min_ratio=args.min_ratio,
            font=args.font, font_file=args.font_file, fontsize=args.size,
            prefer_embedded=not args.no_embedded, allow_download=not args.offline,
        )
        changes = [describe_plan(*plan, args.replacement) for plan in plans]
        _report(changes, args, out=None, dry=True)
        return 0

    target_path = _output_path(args, source)
    changes = replace_hits(
        doc, hits, args.replacement,
        align=args.align, fit=args.fit, min_ratio=args.min_ratio,
        color=color, fill=fill, font=args.font, font_file=args.font_file,
        fontsize=args.size, prefer_embedded=not args.no_embedded,
        allow_download=not args.offline,
    )

    try:  # keep newly embedded fonts from bloating the file
        doc.subset_fonts()
    except Exception:
        pass

    save_pdf(doc, target_path, replacing=source)

    _report(changes, args, out=target_path, dry=False)
    return 0


def save_pdf(doc: fitz.Document, target: Path, replacing: Path) -> None:
    """Write the document, including over its own source file.

    MuPDF only allows saving onto the open file incrementally, which appends a
    revision and leaves the *old text still inside the PDF* — the opposite of
    what a replacement tool should do. So we write a fresh, garbage-collected
    file next to the target and move it into place.
    """
    same_file = target.resolve() == replacing.resolve()
    destination = target.with_name(target.name + ".pdf-retype-tmp") if same_file else target

    try:
        doc.save(str(destination), garbage=4, deflate=True, clean=True)
    except Exception as exc:
        if same_file:
            destination.unlink(missing_ok=True)
        raise SystemExit(f"error: cannot write {target}: {exc}") from exc

    if same_file:
        doc.close()
        try:
            os.replace(destination, target)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise SystemExit(f"error: cannot replace {target}: {exc}") from exc


def _output_path(args: argparse.Namespace, source: Path) -> Path:
    if args.in_place:
        return source
    target = Path(args.output) if args.output else source.with_suffix(".replaced.pdf")
    if target.exists() and not args.force and target != source:
        raise SystemExit(
            f"error: {target} already exists — pass --force to overwrite, or -o to pick another name"
        )
    return target


def _report(changes, args: argparse.Namespace, out: Path | None, dry: bool) -> None:
    if args.json:
        print(json.dumps(
            {
                "target": args.target,
                "replacement": args.replacement,
                "output": str(out) if out else None,
                "dry_run": dry,
                "changes": [c.as_dict() for c in changes],
            },
            indent=2, ensure_ascii=False,
        ))
        return

    verb = "would replace" if dry else "replaced"
    print(f"{verb} {len(changes)} occurrence(s) of {args.target!r} with {args.replacement!r}\n")
    for change in changes:
        score = "" if change.score >= 0.999 else f" (similarity {change.score:.2f})"
        print(f"  p.{change.page + 1}  {change.old_text!r} -> {change.new_text!r}{score}")
        size = f"{change.fontsize:.1f}pt"
        if abs(change.fontsize - change.original_fontsize) > 0.05:
            size += f" (was {change.original_fontsize:.1f}pt)"
        print(f"       drawn with {change.font} {size} — {change.font_note or change.font_source}")
        for warning in change.warnings:
            print(f"       {WARN} {warning}")
    if out:
        print(f"\nSaved to {out}")
    elif dry:
        print("\nDry run — nothing was written.")


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-retype",
        description="Inspect fonts, find words and replace them inside a PDF.",
    )
    parser.add_argument("--version", action="version", version=f"pdf-retype {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--pages", help="pages to look at, e.g. 1,3,5-7 (default: all)")
        sub.add_argument("--json", action="store_true", help="machine-readable output")

    def add_matching(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--fuzzy", action="store_true",
                         help="also match words that merely look similar")
        sub.add_argument("--threshold", type=float, default=0.82,
                         help="similarity needed by --fuzzy, 0-1 (default: 0.82)")
        sub.add_argument("--case-sensitive", action="store_true",
                         help="require the exact casing")
        sub.add_argument("--strict-accents", action="store_true",
                         help="treat 'a' and 'á' as different when matching")

    def add_font_source(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--offline", action="store_true",
                         help="never download a missing font family")

    fonts = subparsers.add_parser("fonts", help="list the fonts a PDF uses")
    fonts.add_argument("pdf")
    fonts.add_argument("--plan", metavar="TEXT",
                       help="show which font each one would be rewritten with, for TEXT")
    add_common(fonts)
    add_font_source(fonts)
    fonts.set_defaults(func=cmd_fonts)

    finder = subparsers.add_parser("find", help="search for a word and show its font")
    finder.add_argument("pdf")
    finder.add_argument("term")
    add_matching(finder)
    add_common(finder)
    finder.set_defaults(func=cmd_find)

    replacer = subparsers.add_parser("replace", help="replace a word, keeping its look")
    replacer.add_argument("pdf")
    replacer.add_argument("target", help="the word to look for")
    replacer.add_argument("replacement", help="what to write instead (empty string deletes)")
    replacer.add_argument("-o", "--output", help="output file (default: <name>.replaced.pdf)")
    replacer.add_argument("--in-place", action="store_true", help="overwrite the source PDF")
    replacer.add_argument("--force", action="store_true", help="overwrite an existing output")
    replacer.add_argument("-n", "--dry-run", action="store_true",
                          help="report what would change without writing")
    replacer.add_argument("--limit", type=int, metavar="N",
                          help="replace at most N occurrences")
    replacer.add_argument("--align", choices=ALIGNMENTS, default="left",
                          help="how to place the new text in the old box (default: left)")
    replacer.add_argument("--fit", choices=FIT_MODES, default="auto",
                          help="when the new text is wider: use free space on the line (auto), "
                               "stay inside the old box (shrink), or keep the size (overflow). "
                               "Default: auto")
    replacer.add_argument("--min-ratio", type=float, default=0.6, metavar="R",
                          help="smallest allowed size when shrinking, as a fraction (default: 0.6)")
    replacer.add_argument("--size", type=float, help="force a font size in points")
    replacer.add_argument("--color", help="force a text colour, e.g. '#c00' or '204,0,0'")
    replacer.add_argument("--fill", help="paint the cleared box this colour before writing")
    replacer.add_argument("--font", metavar="BASE14",
                          help="force a base-14 font name, e.g. helv, tiro, cour")
    replacer.add_argument("--font-file", metavar="PATH",
                          help="force a .ttf/.otf file to draw with")
    replacer.add_argument("--no-embedded", action="store_true",
                          help="ignore the PDF's own embedded font programs")
    add_matching(replacer)
    add_common(replacer)
    add_font_source(replacer)
    replacer.set_defaults(func=cmd_replace)

    cache = subparsers.add_parser("cache", help="show or clear the downloaded font cache")
    cache.add_argument("--clear", action="store_true", help="delete every cached font")
    cache.set_defaults(func=cmd_cache)

    start = subparsers.add_parser(
        "start", help="guided mode: browse for a file, then search and replace step by step"
    )
    start.add_argument("directory", nargs="?", help="folder to start browsing in")
    add_font_source(start)
    start.set_defaults(func=cmd_start)

    return parser


def cmd_start(args: argparse.Namespace) -> int:
    from pdf_retype.interactive import run

    if args.directory:
        start_dir = Path(args.directory).expanduser()
        if not start_dir.is_dir():
            raise SystemExit(f"error: not a folder: {args.directory}")
    else:
        start_dir = Path.cwd()
    return run(start_dir, offline=args.offline)


def cmd_cache(args: argparse.Namespace) -> int:
    directory = cache_dir()
    files = sorted(directory.glob("*.ttf"))
    if args.clear:
        for path in files:
            path.unlink()
        print(f"Removed {len(files)} cached font(s) from {directory}")
        return 0
    print(f"{directory}  ({len(files)} font(s))")
    for path in files:
        print(f"  {path.name}  {path.stat().st_size // 1024} KB")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:  # bare `pdf-retype` starts the guided mode
        from pdf_retype.interactive import run

        return run(Path.cwd())
    try:
        return args.func(args)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
