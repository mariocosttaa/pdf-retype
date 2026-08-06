"""Render the before/after image used in the README.

    python examples/make_preview.py docs/preview.png

Everything here is invented — no real document is used, and none should be:
the point of the picture is the typography, not the data.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import fitz

LINES = [
    ("INVOICE", "hebo", 20, (0.10, 0.10, 0.12)),
    ("Billed to: Alexandra Fontaine", "helv", 13, (0.15, 0.15, 0.18)),
    ("Reference: INV-4820-B", "tiro", 13, (0.15, 0.15, 0.18)),
    ("Amount due: 1.240,00 EUR", "hebo", 13, (0.15, 0.15, 0.18)),
]

SWAPS = [
    ("Alexandra Fontaine", "Béatrice Nogueira"),
    ("INV-4820-B", "INV-9317-C"),
    ("1.240,00", "2.980,50"),
]


def build_source(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=430, height=158)
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    page.draw_line(fitz.Point(34, 62), fitz.Point(396, 62), color=(0.85, 0.85, 0.88), width=1)
    y = 46
    for text, font, size, colour in LINES:
        page.insert_text((34, y), text, fontname=font, fontsize=size, color=colour)
        y += 34
    doc.save(path)


def shot(pdf: Path, png: Path) -> None:
    page = fitz.open(pdf)[0]
    page.get_pixmap(dpi=170).save(png)


def stack(before: Path, after: Path, out: Path) -> None:
    """One image: the original above, the rewritten version below."""
    top, bottom = fitz.Pixmap(before), fitz.Pixmap(after)
    pad, label = 16, 34
    width = max(top.width, bottom.width) + pad * 2
    height = top.height + bottom.height + label * 2 + pad * 3

    canvas = fitz.open()
    page = canvas.new_page(width=width, height=height)
    page.draw_rect(page.rect, color=None, fill=(0.97, 0.97, 0.98))

    y = pad
    for caption, image in (("BEFORE", top), ("AFTER", after and bottom)):
        page.insert_text((pad + 2, y + 16), caption, fontname="hebo", fontsize=11,
                         color=(0.45, 0.45, 0.5))
        y += label - 12
        page.insert_image(fitz.Rect(pad, y, pad + image.width, y + image.height),
                          pixmap=image)
        page.draw_rect(fitz.Rect(pad, y, pad + image.width, y + image.height),
                       color=(0.87, 0.87, 0.9), width=0.8)
        y += image.height + pad + 12

    page.get_pixmap(dpi=110).save(out)


def main(destination: str) -> None:
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source, edited = tmp / "invoice.pdf", tmp / "invoice.edited.pdf"
        build_source(source)

        current = source
        for target, replacement in SWAPS:
            subprocess.run(
                [sys.executable, "-m", "pdf_retype", "replace", str(current),
                 target, replacement, "-o", str(edited), "--force"],
                check=True, capture_output=True,
            )
            current = edited

        before_png, after_png = tmp / "before.png", tmp / "after.png"
        shot(source, before_png)
        shot(edited, after_png)
        stack(before_png, after_png, out)

    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/preview.png")
