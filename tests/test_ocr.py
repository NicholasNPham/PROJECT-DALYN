import pymupdf
import pytest

import ocr


def _blank_pdf(pages: int) -> bytes:
    """A PDF of empty pages, so the text layer is unusable and OCR runs."""
    document = pymupdf.open()
    for _ in range(pages):
        document.new_page()
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture
def ocr_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stand in for Tesseract and count the pages it is handed."""
    calls: list[int] = []

    def fake_image_to_string(image, config: str = "") -> str:
        """Return a line of text for each page, recording the call."""
        calls.append(1)
        return "IN THE CIRCUIT COURT"

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", fake_image_to_string)
    return calls


def test_long_scan_reads_only_the_first_pages(ocr_calls: list[int]) -> None:
    """A scan longer than OCR_PAGE_LIMIT stops at the limit."""
    text, source = ocr.extract_text(_blank_pdf(ocr.OCR_PAGE_LIMIT + 5), "long.pdf")

    assert source == "ocr"
    assert len(ocr_calls) == ocr.OCR_PAGE_LIMIT
    assert "IN THE CIRCUIT COURT" in text


def test_short_scan_reads_every_page(ocr_calls: list[int]) -> None:
    """A scan within the limit is read whole."""
    ocr.extract_text(_blank_pdf(2), "short.pdf")

    assert len(ocr_calls) == 2
