# Contributing

Bug reports and pull requests are welcome.

## Getting set up

```bash
git clone https://github.com/mariocosttaa/pdf-retype
cd pdf-retype
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`python examples/make_sample.py sample.pdf` generates a small PDF with several
base-14 faces to experiment on.

## Reporting a bug

PDFs differ wildly, and most bugs here come from one that is put together
unusually. What helps:

- the command you ran and its full output, including any `!` warnings;
- what `pdf-retype fonts yourfile.pdf --plan "your new text"` says;
- the PDF itself, if you can share it, or one that reproduces the problem.

## Pull requests

- Add a test that fails before your change. The suite in
  `tests/test_pdf_retype.py` builds its own PDFs with PyMuPDF, so a regression
  test rarely needs a fixture file.
- Keep the reporting honest: when the tool cannot reproduce the original exactly,
  it should say so in a warning rather than quietly approximate.
- Match the surrounding style — comments explain *why*, especially where the
  behaviour works around something a PDF or a font does wrong.

## Layout

| Path | What lives there |
| --- | --- |
| `pdf_retype/search.py` | finding text, exact and by similarity |
| `pdf_retype/fonts.py` | describing spans, choosing a font to draw with |
| `pdf_retype/fontlib.py` | font names, system font index, downloads |
| `pdf_retype/replace.py` | redaction, fitting, drawing the new text |
| `pdf_retype/cli.py` | argument parsing and reporting |
| `pdf_retype/interactive.py` | the guided mode |
