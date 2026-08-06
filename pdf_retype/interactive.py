"""Guided mode: pick a file, a word, a replacement, then confirm.

Everything here is stdlib. The list screens (file browser, match picker) run
under ``curses`` so they can be driven with the arrow keys; typing text happens
outside curses, where the terminal handles accents and editing properly.

When there is no terminal to drive — a pipe, a CI job — the same screens fall
back to plain numbered prompts, so the mode still works.
"""

from __future__ import annotations

import curses
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz

from pdf_retype.replace import describe_plan, plan_hits, replace_hits
from pdf_retype.search import find

HOME = Path.home()


@dataclass
class Entry:
    """One row in the file browser."""

    path: Path
    label: str
    is_dir: bool


def list_entries(directory: Path, show_hidden: bool = False) -> list[Entry]:
    """Sub-directories and PDFs of ``directory``, directories first."""
    entries: list[Entry] = []
    if directory.parent != directory:
        entries.append(Entry(directory.parent, "../", True))

    try:
        children = sorted(
            directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except PermissionError:
        return entries

    for child in children:
        if not show_hidden and child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                entries.append(Entry(child, child.name + "/", True))
            elif child.suffix.lower() == ".pdf":
                size = child.stat().st_size
                entries.append(Entry(child, f"{child.name}  ({_human(size)})", False))
        except OSError:
            continue
    return entries


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ------------------------------------------------------------------ curses menu


def _draw(stdscr, title: str, footer: str, rows: list[str], cursor: int, top: int,
          marks: set[int] | None) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    body = max(1, height - 4)

    if cursor < top:
        top = cursor
    elif cursor >= top + body:
        top = cursor - body + 1

    stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "─" * (width - 1), width - 1)

    for offset in range(body):
        index = top + offset
        if index >= len(rows):
            break
        mark = ""
        if marks is not None:
            mark = "[x] " if index in marks else "[ ] "
        text = f" {mark}{rows[index]}"
        attribute = curses.A_REVERSE if index == cursor else curses.A_NORMAL
        stdscr.addnstr(2 + offset, 0, text.ljust(width - 1)[: width - 1], width - 1,
                       attribute)

    stdscr.addnstr(height - 1, 0, footer[: width - 1], width - 1, curses.A_DIM)
    stdscr.refresh()
    return top


_ESCAPE_KEYS = {
    ord("A"): curses.KEY_UP,
    ord("B"): curses.KEY_DOWN,
    ord("C"): curses.KEY_RIGHT,
    ord("D"): curses.KEY_LEFT,
    ord("H"): curses.KEY_HOME,
    ord("F"): curses.KEY_END,
}


def _read_key(stdscr) -> int:
    """One keypress, with raw arrow sequences folded into curses key codes.

    ``keypad(True)`` puts the terminal in application mode, where arrows arrive
    as ``ESC O B`` and ncurses decodes them for us. Not every terminal honours
    that — tmux and some emulators still send ``ESC [ B`` — and there the bare
    ESC would read as "cancel". So we decode the sequence ourselves and only
    report ESC when nothing follows it.
    """
    key = stdscr.getch()
    if key != 27:
        return key

    stdscr.timeout(60)
    try:
        second = stdscr.getch()
        if second == -1:
            return 27  # a genuine, lone Escape
        third = stdscr.getch()
    finally:
        stdscr.timeout(-1)

    if second in (ord("["), ord("O")):
        return _ESCAPE_KEYS.get(third, -1)
    return 27


def _menu(stdscr, title: str, rows: list[str], footer: str,
          marks: set[int] | None = None):
    """Generic list screen. Returns (action, cursor) where action is a key name."""
    curses.curs_set(0)
    stdscr.keypad(True)
    # Arrow keys arrive as ESC [ A. Without a short escape delay ncurses hands us
    # a bare ESC first, and every arrow press would read as "cancel".
    try:
        curses.set_escdelay(50)
    except (AttributeError, curses.error):
        pass
    cursor, top = 0, 0
    while True:
        top = _draw(stdscr, title, footer, rows, cursor, top, marks)
        key = _read_key(stdscr)

        if key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(rows) - 1, cursor + 1)
        elif key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_NPAGE:
            cursor = min(len(rows) - 1, cursor + 10)
        elif key == curses.KEY_PPAGE:
            cursor = max(0, cursor - 10)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(rows) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return "enter", cursor
        elif key == ord(" "):
            return "space", cursor
        elif key in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
            return "back", cursor
        elif key in (27, ord("q")):
            return "quit", cursor
        elif key in (ord("a"), ord("n")):
            return chr(key), cursor
        elif key == ord("~"):
            return "home", cursor


# ------------------------------------------------------------------ file picker


def browse(start: Path) -> Path | None:
    """Walk the filesystem and return the chosen PDF."""
    if not has_tty():
        return _browse_plain(start)
    try:
        return curses.wrapper(_browse_curses, start)
    except curses.error:
        return _browse_plain(start)


def _browse_curses(stdscr, start: Path) -> Path | None:
    current = start
    while True:
        entries = list_entries(current)
        rows = [entry.label for entry in entries] or ["(no PDFs or folders here)"]
        title = f"Choose a PDF — {_shorten(current)}"
        footer = "↑↓ move   ⏎ open/select   ← up   ~ home   q cancel"

        action, index = _menu(stdscr, title, rows, footer)

        if action == "quit":
            return None
        if action == "home":
            current = HOME
            continue
        if action == "back":
            current = current.parent
            continue
        if action == "enter" and entries:
            chosen = entries[index]
            if chosen.is_dir:
                current = chosen.path
            else:
                return chosen.path


def _browse_plain(start: Path) -> Path | None:
    """Numbered fallback for terminals curses cannot drive."""
    current = start
    while True:
        entries = list_entries(current)
        print(f"\n{current}")
        for number, entry in enumerate(entries, 1):
            print(f"  {number:>3}) {entry.label}")
        answer = input("number, path, or 'q' to cancel: ").strip()
        if answer.lower() == "q":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(entries):
            chosen = entries[int(answer) - 1]
            if chosen.is_dir:
                current = chosen.path
            else:
                return chosen.path
            continue
        candidate = Path(answer).expanduser()
        if candidate.is_dir():
            current = candidate
        elif candidate.is_file():
            return candidate
        else:
            print("  not found")


def _shorten(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(HOME))
    except ValueError:
        return str(path)


# ----------------------------------------------------------------- match picker


def describe_hit(hit) -> str:
    similar = f"  ({hit.score:.0%} similar)" if hit.score < 0.999 else ""
    return (
        f"p.{hit.page + 1}  {hit.text!r}{similar}"
        f"   [{hit.style['font']} {hit.style['size']}pt]"
    )


def choose_hits(hits, term: str) -> list[int] | None:
    """Let the user untick matches. Returns the indices to act on."""
    if len(hits) == 1:
        # No point clearing the screen for a picker with a single row.
        print(f"  {describe_hit(hits[0])}\n")
        return [0]

    rows = [describe_hit(hit) for hit in hits]
    marks = set(range(len(hits)))

    if not has_tty():
        return list(marks)
    try:
        return curses.wrapper(_choose_curses, rows, marks, term)
    except curses.error:
        return list(marks)


def _choose_curses(stdscr, rows: list[str], marks: set[int], term: str) -> list[int] | None:
    while True:
        title = f"{len(marks)} of {len(rows)} matches for {term!r} selected"
        footer = "↑↓ move   space toggle   a all   n none   ⏎ continue   q cancel"
        action, index = _menu(stdscr, title, rows, footer, marks)

        if action == "quit":
            return None
        if action == "space":
            marks.symmetric_difference_update({index})
        elif action == "a":
            marks.update(range(len(rows)))
        elif action == "n":
            marks.clear()
        elif action == "enter":
            return sorted(marks)


# ------------------------------------------------------------------- text input


def ask(prompt: str, default: str = "", allow_empty: bool = False) -> str | None:
    """Read a line. Returns None when the user aborts."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        if default:
            return default
        if allow_empty:
            return ""
        return None
    return answer


def confirm(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes", "s", "sim")


# ------------------------------------------------------------------------- flow


@dataclass
class Edit:
    """One applied round, for the closing summary."""

    term: str
    replacement: str
    count: int


def run(start: Path | None = None, offline: bool = False) -> int:
    """The whole guided session. Returns a process exit code."""
    print("pdf-retype — guided mode\n")

    source = browse(start or Path.cwd())
    if source is None:
        print("Cancelled.")
        return 1

    doc = _open(source)
    if doc is None:
        return 1

    print(f"\nFile:  {source}")
    print(f"Pages: {doc.page_count}\n")

    opened_from = source  # where the document in memory currently lives
    target: Path | None = None
    history: list[Edit] = []

    while True:
        outcome = _one_round(doc, source, target, opened_from, history, offline)
        if outcome is None:
            break
        target, applied = outcome

        if not applied:  # nothing written this round
            if confirm("Try a different change?", default=True):
                print()
                continue
            break

        if not confirm("\nChange something else in this file?", default=True):
            break

        # Reopen what we just wrote. Carrying the in-memory document forward
        # would mean editing a document whose fonts subset_fonts() has already
        # pruned, and the next round could find glyphs missing.
        if not doc.is_closed:
            doc.close()
        doc = _open(target)
        if doc is None:
            break
        opened_from = target
        print()

    _summary(history, target)
    return 0 if history else 1


def _one_round(doc, source: Path, target: Path | None, opened_from: Path,
               history: list[Edit], offline: bool):
    """One find-replace-save cycle.

    Returns ``None`` to end the session, otherwise ``(target, applied)``.
    """
    hits, term = _search_loop(doc)
    if hits is None:
        return None

    keep = choose_hits(hits, term)
    if keep is None or not keep:
        print("Nothing selected.")
        return target, False
    hits = [hits[index] for index in keep]

    replacement = ask(f"Replace {term!r} with", allow_empty=True)
    if replacement is None:
        return target, False

    print()
    plans = plan_hits(doc, hits, replacement, allow_download=not offline)
    _preview([describe_plan(*plan, replacement) for plan in plans], term, replacement)

    if not confirm("\nApply these changes?"):
        print("Skipped — nothing was written.")
        return target, False

    if target is None:  # only asked once per session
        target = _ask_target(source)
        if target is None:
            return None

    replace_hits(doc, hits, replacement, allow_download=not offline)
    try:
        doc.subset_fonts()
    except Exception:
        pass

    from pdf_retype.cli import save_pdf  # imported late: cli imports this module

    save_pdf(doc, target, replacing=opened_from)
    history.append(Edit(term, replacement, len(hits)))
    print(f"\nSaved — {len(hits)} replacement(s) written to {target}")
    return target, True


def _open(path: Path):
    try:
        doc = fitz.open(path)
    except Exception as exc:
        print(f"error: cannot open {path}: {exc}")
        return None
    if doc.needs_pass:
        print(f"error: {path.name} is password protected")
        return None
    return doc


def _ask_target(source: Path) -> Path | None:
    """Where to write — asked once, then reused for the rest of the session."""
    default_out = source.with_suffix(".replaced.pdf")
    answer = ask("Save as (or 'same' to overwrite the original)", default=str(default_out))
    if answer is None:
        return None

    if answer.strip().lower() == "same":
        if not confirm(f"Overwrite {source.name} itself?"):
            return None
        return source

    target = Path(answer).expanduser()
    if target.exists() and not confirm(f"{target.name} exists. Overwrite?"):
        return None
    return target


def _summary(history: list[Edit], target: Path | None) -> None:
    if not history:
        print("\nNothing was changed.")
        return
    total = sum(edit.count for edit in history)
    print(f"\n{len(history)} change(s), {total} replacement(s) in total:")
    for number, edit in enumerate(history, 1):
        print(f"  {number}. {edit.term!r} -> {edit.replacement!r}  ({edit.count}×)")
    print(f"\nFile: {target}")


def _search_loop(doc: fitz.Document):
    """Ask for a word until something is found (or the user gives up)."""
    while True:
        term = ask("Word or phrase to find")
        if term is None:
            return None, ""

        hits = find(doc, term)
        if hits:
            print(f"  found {len(hits)} exact match(es)\n")
            return hits, term

        print(f"  no exact match for {term!r}")
        if confirm("  search for similar words instead?", default=True):
            hits = find(doc, term, fuzzy=True, threshold=0.7)
            if hits:
                print(f"  found {len(hits)} similar match(es)\n")
                return hits, term
            print("  nothing similar either")

        if not confirm("  try another word?", default=True):
            return None, ""


def _preview(changes, term: str, replacement: str) -> None:
    print(f"About to replace {term!r} with {replacement!r}:\n")
    for change in changes:
        print(f"  p.{change.page + 1}  {change.old_text!r} -> {change.new_text!r}")
        size = f"{change.fontsize:.1f}pt"
        if abs(change.fontsize - change.original_fontsize) > 0.05:
            size += f" (was {change.original_fontsize:.1f}pt)"
        print(f"       {change.font} {size} — {change.font_note or change.font_source}")
        for warning in change.warnings:
            print(f"       ! {warning}")
