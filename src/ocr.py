"""Pulls text out of PDF attachments. Tries embedded text first, OCR only if needed."""

import io

import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="torch")

import easyocr
import pymupdf
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from exceptions import DocumentProblem
from logger import get_logger

# Below this many characters per page, a PDF is treated as a scan rather than
# a digital document. Digital orders from the e-filing portal run in the
# thousands; a scan with no text layer returns near zero.
MIN_CHARS_PER_PAGE = 50

# Render resolution for OCR. 300 is the practical floor for reliable results
# on printed text; higher costs time and memory for little gain.
OCR_DPI = 300

_READER = None

logger = get_logger(__name__)


def extract_text(pdf_bytes: bytes, filename: str) -> str:
    """Return the text of a PDF, using OCR only when there is no text layer.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Attachment name, used for logging and error messages only.

    Returns:
        The document's text. Whitespace is preserved as extracted.

    Raises:
        DocumentProblem: If the PDF cannot be read, is encrypted, or produces
            no usable text even after OCR.
    """
    text, page_count = _extract_embedded(pdf_bytes, filename)

    if _has_usable_text(text, page_count):
        logger.debug(
            "%s: %s chars from %s pages, embedded text", filename, len(text), page_count
        )
        return text

    logger.info(
        "%s: only %s chars from %s pages, needs OCR", filename, len(text), page_count
    )
    return _ocr(pdf_bytes, filename)


def _extract_embedded(pdf_bytes: bytes, filename: str) -> tuple[str, int]:
    """Read the PDF's own text layer.

    Returns:
        The extracted text and the page count. Text may be empty for a scan.

    Raises:
        DocumentProblem: If the file is not a readable PDF.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except PdfReadError as error:
        raise DocumentProblem(f"{filename}: not a readable PDF: {error}") from error

    if reader.is_encrypted:
        # Some PDFs are "encrypted" with an empty owner password and open fine.
        try:
            if reader.decrypt("") == 0:
                raise DocumentProblem(f"{filename}: password protected")
        except (NotImplementedError, PdfReadError) as error:
            raise DocumentProblem(
                f"{filename}: uses unsupported encryption: {error}"
            ) from error

    pages = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as error:
            # One malformed page should not lose the rest of the document.
            logger.warning(
                "%s: page %s could not be read (%s), continuing",
                filename,
                number,
                type(error).__name__,
            )
            pages.append("")

    return "\n".join(pages), len(reader.pages)


def _has_usable_text(text: str, page_count: int) -> bool:
    """Decide whether the embedded text is real content or an empty text layer."""
    if page_count == 0:
        return False
    return len(text.strip()) >= MIN_CHARS_PER_PAGE * page_count


def _ocr(pdf_bytes: bytes, filename: str) -> str:
    """Read a scanned PDF by rendering its pages and running OCR on them.

    Slow compared to embedded text extraction, roughly a few seconds per page.
    Only reached when a document has no usable text layer.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Attachment name, for logging and error messages.

    Returns:
        The OCR'd text, pages joined by newlines.

    Raises:
        DocumentProblem: If the PDF cannot be rendered or OCR produces nothing.
    """
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as error:
        raise DocumentProblem(
            f"{filename}: could not open for OCR: {type(error).__name__}: {error}"
        ) from error

    reader = _get_reader()
    pages = []

    with document:
        for number, page in enumerate(document, start=1):
            try:
                pixmap = page.get_pixmap(dpi=OCR_DPI)
                lines = reader.readtext(pixmap.tobytes("png"), detail=0, paragraph=True)
                pages.append("\n".join(lines))
            except Exception as error:
                logger.warning(
                    "%s: OCR failed on page %s (%s), continuing",
                    filename,
                    number,
                    type(error).__name__,
                )
                pages.append("")

    text = "\n".join(pages)

    if not text.strip():
        raise DocumentProblem(
            f"{filename}: OCR produced no text from {len(pages)} pages"
        )

    logger.info("%s: %s chars from %s pages via OCR", filename, len(text), len(pages))
    return text


def _get_reader() -> easyocr.Reader:
    """Return the shared OCR reader, building it on first use.

    Loading the model takes several seconds and a few hundred MB of memory,
    so it is built once and reused for the life of the process rather than
    per document.

    Raises:
        DocumentProblem: If the model cannot be loaded.
    """
    global _READER

    if _READER is None:
        try:
            logger.info("Loading OCR model, first use only")
            _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
        except Exception as error:
            raise DocumentProblem(
                f"OCR model unavailable: {type(error).__name__}: {error}"
            ) from error

    return _READER