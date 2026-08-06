import re
from pathlib import Path

import fitz
import pytest

from pdf_retype.cli import parse_pages, save_pdf
from pdf_retype.fontlib import parse_font_name, serif_from_program
from pdf_retype.fonts import base14_for, list_fonts
from pdf_retype.interactive import describe_hit, list_entries
from pdf_retype.replace import (
    free_width,
    parse_color,
    redaction_rect,
    replace_hits,
)
from pdf_retype.search import find, normalize, similarity


@pytest.fixture
def doc():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 100), "Passenger: Alfredo Costa", fontname="hebo", fontsize=14)
    page.insert_text((60, 130), "Booking reference: XRBYVW", fontname="helv", fontsize=11)
    page.insert_text((60, 160), "Total: 1.077,97 EUR", fontname="tiro", fontsize=11)
    return document


# ------------------------------------------------------------------ font names


@pytest.mark.parametrize(
    "raw,family,weight,italic",
    [
        ("AAAAAB+GTFlexa-Bold", "GT Flexa", 700, False),
        ("AAAAAI+Inter-SemiBold", "Inter", 600, False),
        ("Arial Unicode MS+Arial Unicode MS", "Arial Unicode MS", 400, False),
        ("ABCDEF+Roboto-BoldItalic", "Roboto", 700, True),
        ("TimesNewRomanPS-BoldMT", "Times New Roman", 700, False),
        ("Geist-ExtraLight", "Geist", 200, False),
        ("Helvetica", "Helvetica", 400, False),
    ],
)
def test_parse_font_name(raw, family, weight, italic):
    ident = parse_font_name(raw)
    assert (ident.family, ident.weight, ident.italic) == (family, weight, italic)


def test_subset_tag_is_stripped_but_plain_names_survive():
    assert parse_font_name("ABCDEF+Inter").family == "Inter"
    assert parse_font_name("Inter").family == "Inter"


def test_serif_detection_reads_the_font_program():
    times = Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf")
    arial = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if not (times.is_file() and arial.is_file()):
        pytest.skip("system fonts unavailable")
    assert serif_from_program(times.read_bytes()) is True
    assert serif_from_program(arial.read_bytes()) is False


def test_pdf_serif_flag_does_not_override_a_sans_name():
    """A lying /Flags bit must not turn a grotesque into Times."""
    style = {
        "raw_font": "GTFlexa-Bold", "font": "GTFlexa-Bold", "serif": True,
        "monospace": False, "bold": True, "italic": False,
    }
    assert base14_for(style) == "hebo"


# --------------------------------------------------------------------- matching


def test_normalize_folds_case_and_accents():
    assert normalize("Sá,") == "sa"
    assert normalize("SÃO") == "sao"


def test_similarity_scores_near_matches_above_distant_ones():
    assert similarity("Alfredo", "Alfred") > similarity("Alfredo", "Mariana")


def test_find_exact(doc):
    hits = find(doc, "Alfredo")
    assert len(hits) == 1
    assert hits[0].page == 0
    assert hits[0].score == 1.0
    assert hits[0].style["size"] == 14.0
    assert hits[0].style["bold"] is True


def test_find_returns_nothing_for_absent_text(doc):
    assert find(doc, "Mariana") == []


def test_find_fuzzy_catches_a_misspelling(doc):
    hits = find(doc, "Alfred Kosta", fuzzy=True, threshold=0.7)
    assert [h.text for h in hits] == ["Alfredo Costa"]
    assert 0.7 <= hits[0].score < 1.0


def test_fuzzy_ignores_accents_by_default():
    document = fitz.open()
    document.new_page().insert_text((60, 100), "Sao Paulo", fontname="helv", fontsize=11)
    assert find(document, "São Paulo", fuzzy=True, threshold=0.9)


def test_fuzzy_deduplicates_overlapping_ngrams(doc):
    hits = find(doc, "Alfredo Costa", fuzzy=True, threshold=0.6)
    assert len(hits) == 1


# -------------------------------------------------------------------- replacing


def test_replace_swaps_the_text_and_keeps_the_rest(doc):
    before = {line.strip() for line in doc[0].get_text().splitlines() if line.strip()}
    changes = replace_hits(doc, find(doc, "Alfredo Costa"), "Mariana Pereira")
    after = {line.strip() for line in doc[0].get_text().splitlines() if line.strip()}

    assert len(changes) == 1
    assert "Mariana Pereira" in " ".join(after)
    assert "Alfredo Costa" not in " ".join(after)
    # every untouched line survives
    assert {"Booking reference: XRBYVW", "Total: 1.077,97 EUR"} <= after
    assert before - after == {"Passenger: Alfredo Costa"}


def test_replace_keeps_size_when_the_line_has_room(doc):
    changes = replace_hits(doc, find(doc, "Alfredo"), "Ana", fit="auto")
    assert changes[0].fontsize == changes[0].original_fontsize


def test_shrink_mode_stays_inside_the_original_box(doc):
    changes = replace_hits(doc, find(doc, "Alfredo"), "Maria Madalena", fit="shrink")
    assert changes[0].fontsize < changes[0].original_fontsize


def test_min_ratio_is_a_floor(doc):
    changes = replace_hits(
        doc, find(doc, "Alfredo"), "A" * 120, fit="shrink", min_ratio=0.8
    )
    assert changes[0].fontsize == pytest.approx(changes[0].original_fontsize * 0.8)
    assert any("does not fit" in w for w in changes[0].warnings)


def test_overflow_mode_never_resizes(doc):
    changes = replace_hits(doc, find(doc, "Alfredo"), "A" * 60, fit="overflow")
    assert changes[0].fontsize == changes[0].original_fontsize


def test_empty_replacement_deletes(doc):
    replace_hits(doc, find(doc, "XRBYVW"), "")
    assert "XRBYVW" not in doc[0].get_text()


def test_redaction_rect_does_not_reach_the_next_line():
    """Search boxes span the line height and can clip the line below."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 100), "ZKMEGS", fontname="helv", fontsize=12)
    page.insert_text((60, 112), "2525978392", fontname="helv", fontsize=10)

    hit = find(document, "ZKMEGS")[0]
    trimmed = redaction_rect(page, hit)
    neighbour = fitz.Rect(page.search_for("2525978392")[0])
    assert (trimmed & neighbour).is_empty

    replace_hits(document, [hit], "QW7X2P")
    assert "2525978392" in page.get_text()


def test_replaced_text_can_be_found_again(doc):
    """A replacement must survive extraction as the characters we asked for.

    Fonts that share one glyph between U+0020 and U+00A0 otherwise yield
    'Mariana\\xa0Pereira', which no later search would match — fatal when
    editing the same file repeatedly.
    """
    replace_hits(doc, find(doc, "Alfredo Costa"), "Mariana Pereira")

    assert "\xa0" not in doc[0].get_text()
    assert doc[0].search_for("Mariana Pereira")
    assert find(doc, "Mariana Pereira")


def test_multiple_spaces_are_preserved(doc):
    replace_hits(doc, find(doc, "Alfredo"), "A  B")
    assert "A  B" in doc[0].get_text()


def test_replacement_reports_the_font_it_used(doc):
    change = replace_hits(doc, find(doc, "Alfredo"), "Ana")[0]
    assert change.font_source in {"embedded", "system", "downloaded", "base14"}
    assert change.font_note
    # a hashed resource id would help nobody
    assert not re.fullmatch(r"F[0-9a-f]{10}", change.font)


def test_a_word_mid_span_does_not_overrun_the_next_word():
    """Free space is bounded by the next word, not by the enclosing span."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (60, 100), "Use the reference below to pay", fontname="helv", fontsize=12
    )
    hit = find(document, "reference")[0]
    following = page.search_for("below")[0]

    room = free_width(page, hit.rect)
    assert room < page.rect.x1 - hit.rect.x0  # bounded by "below", not the margin
    assert hit.rect.x0 + room <= following.x0

    # Allowed to shrink far enough, the replacement stays clear of the next word.
    replace_hits(document, [hit], "reference number MB", min_ratio=0.3)
    assert page.search_for("reference number MB")[0].x1 <= following.x0 + 0.5


def test_overrunning_the_size_floor_is_reported():
    """When --min-ratio forbids shrinking enough, say so rather than hide it."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 100), "Use the reference below to pay", fontname="helv", fontsize=12)

    changes = replace_hits(
        document, find(document, "reference"), "reference number MB", min_ratio=0.6
    )
    assert any("does not fit" in w for w in changes[0].warnings)


def test_in_place_save_leaves_no_trace_of_the_old_text(doc, tmp_path):
    """Incremental saves would keep the original text inside the file."""
    path = tmp_path / "doc.pdf"
    doc.save(path)

    document = fitz.open(path)
    replace_hits(document, find(document, "XRBYVW"), "QW7X2P")
    save_pdf(document, path, replacing=path)

    assert not list(tmp_path.glob("*tmp*"))

    text = fitz.open(path)[0].get_text()
    assert "QW7X2P" in text
    assert "XRBYVW" not in text

    # An incremental save appends a revision, keeping the old page reachable.
    raw = path.read_bytes()
    assert raw.count(b"%%EOF") == 1
    assert b"/Prev" not in raw


# ----------------------------------------------------------------------- fonts


def test_list_fonts_reports_each_face(doc):
    families = {use.family for use in list_fonts(doc)}
    assert {"Helvetica-Bold", "Helvetica", "Times-Roman"} & families


# --------------------------------------------------------------- guided mode


def test_list_entries_shows_folders_and_pdfs_only(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / ".hidden.pdf").write_bytes(b"%PDF-1.7\n")

    labels = [entry.label for entry in list_entries(tmp_path)]

    assert labels[0] == "../"           # always a way back up
    assert "sub/" in labels             # folders before files
    assert any(label.startswith("a.pdf") for label in labels)
    assert not any("notes.txt" in label for label in labels)
    assert not any("hidden" in label for label in labels)


def test_list_entries_can_show_hidden(tmp_path):
    (tmp_path / ".secret.pdf").write_bytes(b"%PDF-1.7\n")
    labels = [e.label for e in list_entries(tmp_path, show_hidden=True)]
    assert any(".secret.pdf" in label for label in labels)


def test_list_entries_at_the_root_has_no_parent_row():
    assert all(entry.label != "../" for entry in list_entries(Path("/")))


def test_guided_mode_works_without_curses(monkeypatch):
    """Windows ships no _curses in the stdlib, and the module must still load."""
    import builtins
    import importlib

    import pdf_retype.interactive as interactive

    real_import = builtins.__import__

    def without_curses(name, *args, **kwargs):
        if name == "curses":
            raise ImportError("no _curses on this platform")
        return real_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", without_curses)
        reloaded = importlib.reload(interactive)
        assert reloaded.curses is None
        assert reloaded.can_draw_screens() is False  # falls back to prompts
        assert reloaded.list_entries(Path.cwd()) is not None
    finally:
        monkeypatch.undo()
        importlib.reload(interactive)


def test_describe_hit_mentions_page_font_and_similarity(doc):
    exact = describe_hit(find(doc, "Alfredo")[0])
    assert exact.startswith("p.1")
    assert "Helvetica-Bold" in exact
    assert "similar" not in exact

    fuzzy = describe_hit(find(doc, "Alfred Kosta", fuzzy=True, threshold=0.7)[0])
    assert "similar" in fuzzy


# ------------------------------------------------------------------ cli helpers


@pytest.mark.parametrize(
    "spec,expected",
    [("1", [0]), ("1,3", [0, 2]), ("2-4", [1, 2, 3]), ("1,1", [0]), (None, None)],
)
def test_parse_pages(spec, expected):
    assert parse_pages(spec, 10) == expected


def test_parse_pages_rejects_out_of_range():
    with pytest.raises(ValueError):
        parse_pages("0", 5)
    with pytest.raises(ValueError):
        parse_pages("4-99", 5)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#ffffff", (1.0, 1.0, 1.0)),
        ("000000", (0.0, 0.0, 0.0)),
        ("#f00", (1.0, 0.0, 0.0)),
        ("255,0,0", (1.0, 0.0, 0.0)),
        ("1,0,0", (1.0, 0.0, 0.0)),
    ],
)
def test_parse_color(value, expected):
    assert parse_color(value) == pytest.approx(expected)


def test_parse_color_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_color("nope")
