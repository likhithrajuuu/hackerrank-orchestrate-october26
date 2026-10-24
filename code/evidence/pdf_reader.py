from pathlib import Path


def read_pdf(path: Path):
    """Yield (page_no, page_text) for each page that has an extractable text layer."""
    from pypdf import PdfReader  # imported lazily: only needed for PDFs

    for page_no, page in enumerate(PdfReader(str(path)).pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            yield page_no, text
