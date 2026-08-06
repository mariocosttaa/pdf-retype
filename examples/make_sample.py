"""Generate a small sample PDF to try the commands against.

    python examples/make_sample.py sample.pdf
"""

import sys

import fitz

LINES = [
    ("Invoice for Alfredo Costa", "hebo", 16),
    ("Booking reference: XRBYVW", "helv", 12),
    ("Passenger: Mr Alfredo Costa", "tibo", 12),
    ("Departure: Luanda to Lisboa", "tiro", 12),
    ("Total: 1.077,97 EUR", "cobo", 12),
    ("Terms in small print apply", "coit", 9),
]


def build(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    y = 90
    for text, font, size in LINES:
        page.insert_text((60, y), text, fontname=font, fontsize=size)
        y += size * 2.2
    page.draw_rect(fitz.Rect(50, 60, 545, y - 10), color=(0.7, 0.7, 0.7), width=0.8)
    doc.save(path, garbage=4, deflate=True)
    print(f"wrote {path}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
