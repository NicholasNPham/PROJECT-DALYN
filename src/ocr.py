"""Pulls text out of PDF attachments. Tries embedded text first, OCR only if needed.

OCR is Tesseract, reached through pytesseract. Tesseract is a separate program,
not a Python package: pip install pytesseract only installs the wrapper. On
Windows, install the binary from https://github.com/UB-Mannheim/tesseract/wiki
and either add it to PATH or set paths.tesseract in config.yaml.
"""

import io
import logging
import re

import warnings
import cv2
import numpy
import pymupdf
import pytesseract
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from exceptions import DocumentProblem, SystemProblem
from logger import get_logger

warnings.filterwarnings("ignore", category=UserWarning)

# pypdf prints "Ignoring wrong pointing object 6 0 (offset 0)" for any PDF whose
# cross-reference table disagrees with where the objects actually are. It then
# recovers by scanning the file, and the read succeeds. E-filed PDFs are rebuilt
# and appended to repeatedly, so this is routine and says nothing about whether
# the document is readable. Silenced because it buries DALYN's own log lines.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Below this many characters per page, a PDF is treated as a scan rather than
# a digital document. Digital orders from the e-filing portal run in the
# thousands; a scan with no text layer returns near zero.
MIN_CHARS_PER_PAGE = 50

# Some PDFs carry a text layer that decodes to nonsense: the embedded font has a
# broken character map, so pypdf returns what amounts to a substitution cipher.
# Batch 3's 011_Motion For Return Of Property came back as "wnoti qenw jq
# gnttnwq" where the page plainly reads "would show as follows". The character
# count looks healthy and the characters are ordinary letters, so neither a
# length check nor a punctuation check catches it. What does catch it is that
# none of the words are words: a real filing is full of "the", "of", "court",
# "defendant".
#
# Single-letter tokens are excluded from the count, because a broken font map
# produces them by the hundred and they drown the signal. That exclusion does
# most of the work: with it, corrupt documents score 0.020 or below.
#
# The floor is deliberately low. A first attempt at 0.25 rejected real
# documents: a psychological evaluation or a police report is narrative prose
# and uses far less court vocabulary than a motion, scoring 0.17 to 0.23, and
# was being sent to OCR for nothing. Measured across the samples, corrupt tops
# out at 0.020 and the worst genuine document sits at 0.17, so 0.10 has room
# on both sides.
MIN_COMMON_WORD_RATE = 0.10
MIN_WORDS_TO_JUDGE = 20

# A third way a text layer fails: it decodes to real words, but emits every
# word on its own line. Batch 5's 035_Notice Of Taking Deposition came back as
# 217 lines reading "in", "the", "circuit", "court", one word each. The words
# are perfect and the document is unclassifiable, because this classifier
# matches on the START of a line and no line is a title any more.
#
# Measured over 60 sample PDFs: every word-per-line document scored 0.09 or
# below, and every correctly structured one scored 4.02 or above.
MIN_WORDS_PER_LINE = 2.0

# Everyday English first, then the vocabulary court filings repeat. The
# everyday half matters: a report or a letter is prose, and a list of only
# court terms scores those documents as though they were corrupt. This does not
# need to be long or clever. It only has to be words a working text layer
# cannot avoid producing.
COMMON_WORDS = frozenset({
    "the", "of", "and", "to", "in", "is", "that", "for", "on", "this",
    "by", "with", "as", "be", "at", "it", "was", "were", "will", "not",
    "from", "have", "has", "had", "they", "their", "been", "which", "are",
    "or", "an", "his", "her", "him", "he", "she", "all", "any", "may",
    "can", "said", "upon", "one", "no", "if", "but", "out", "about",
    "after", "before", "more", "than", "when", "where", "who", "what",
    "there", "these", "those", "such", "also", "would", "should", "could",
    "into", "over", "under", "time", "date", "page", "them", "we", "you",
    "do", "does", "did", "so", "up", "other", "only", "some", "then",
    "court", "county", "florida", "state", "defendant", "case", "attorney",
    "counsel", "hereby", "shall", "motion", "order", "notice", "judicial",
    "circuit", "plaintiff", "filed", "service", "honorable",
})

# Render resolution for OCR. 300 is the practical floor for reliable results
# on printed text; higher costs time and memory for little gain.
OCR_DPI = 300

# Tesseract page segmentation mode 3 is its default: analyze the page layout
# and return text in reading order, one line per line. That layout analysis is
# the reason it is used here rather than a detector that returns loose boxes:
# this classifier matches on the START of a line, so line boundaries have to be
# the document's real ones.
TESSERACT_CONFIG = "--psm 3"

# Title lines in these filings are underlined, and on a scan the underline
# touches the bottom of the letters. OCR then sees false descenders and closed
# bowls: every O becomes a q and every U becomes an l. A real example from
# batch 5's 001_Motion, at 300 DPI:
#
#   printed: COUNSEL FOR THE DEFENDANTS AMENDED AND UNOPPOSED MOTION TO APPEAR
#   OCR:     cqlnsel fqr thedefendants amendedandlnnopposedmqtion tq appear
#
# The body text of the same page, which is not underlined, reads perfectly.
# Erasing long horizontal runs before OCR fixes it outright.
#
# A run this wide cannot be part of a letter. At 300 DPI, 120 pixels is 0.4
# inches, and the widest glyph on a letter-size page is a fraction of that. What
# it does remove, besides underlines, is table rules, letterhead dividers and
# signature lines, none of which carry text.
UNDERLINE_MIN_WIDTH = 120
UNDERLINE_THICKEN = 3

# The retry exists to re-read a corrupt heading, and a heading is on page one.
# Rasterising a 60-page transcript to find one is pure waste: batch 9's 20-page
# memorandum cost 41 seconds and still matched nothing.
FORCE_OCR_PAGE_LIMIT = 3

logger = get_logger(__name__)


def configure_tesseract(tesseract_path=None) -> None:
    """Point pytesseract at the Tesseract binary. Call once at startup.

    Args:
        tesseract_path: Full path to tesseract.exe. Pass None to rely on PATH.

    Raises:
        SystemProblem: If Tesseract cannot be run. This is an environment
            problem, not a document problem, so it stops DALYN rather than
            sending one email to Manual Review.
    """
    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

    try:
        version = pytesseract.get_tesseract_version()
    except Exception as error:
        raise SystemProblem(
            "Tesseract is not installed or not on PATH. Install it from "
            "https://github.com/UB-Mannheim/tesseract/wiki and set paths.tesseract "
            f"in config.yaml to tesseract.exe. ({type(error).__name__}: {error})"
        ) from error

    logger.info("Tesseract %s ready", version)


def extract_text(pdf_bytes: bytes, filename: str, force_ocr: bool = False) -> tuple[str, str]:
    """Return the text of a PDF, using OCR only when the text layer is unusable.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Attachment name, used for logging and error messages only.
        force_ocr: Skip the text layer and go straight to OCR. Used to retry a
            document that classified to nothing, which happens when a heading is
            corrupt but the body is clean enough to pass the usability checks.
            Only the first FORCE_OCR_PAGE_LIMIT pages are read, since that retry
            is only ever looking for a heading.

    Returns:
        A tuple of the document's text and where it came from, either
        "embedded" or "ocr". Whitespace is preserved as extracted.

    Raises:
        DocumentProblem: If the PDF cannot be read, is encrypted, or produces
            no usable text even after OCR.
        SystemProblem: If Tesseract itself is unavailable.
    """
    if force_ocr:
        logger.info(
            "%s: forced OCR of the first %s pages, skipping the text layer",
            filename,
            FORCE_OCR_PAGE_LIMIT,
        )
        return _ocr(pdf_bytes, filename, page_limit=FORCE_OCR_PAGE_LIMIT), "ocr"

    text, page_count = _extract_embedded(pdf_bytes, filename)

    if _has_usable_text(text, page_count):
        logger.debug(
            "%s: %s chars from %s pages, embedded text", filename, len(text), page_count
        )
        return text, "embedded"

    logger.info(
        "%s: embedded text unusable (%s chars from %s pages), running OCR",
        filename,
        len(text),
        page_count,
    )
    return _ocr(pdf_bytes, filename), "ocr"


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
    """Decide whether the embedded text is real content, or OCR is needed.

    Two ways a text layer fails. It can be absent, which the character count
    catches, or it can be present and decode to nonsense, which only reading
    the words catches. A document that fails either way goes to OCR.
    """
    if page_count == 0:
        return False

    if len(text.strip()) < MIN_CHARS_PER_PAGE * page_count:
        return False

    return _is_readable(text)


def _is_readable(text: str) -> bool:
    """Reject a text layer that decoded to nonsense, or lost its line structure.

    Three checks, because a text layer fails in three different ways and only
    one of them is visible in the character count.

    Too few words means there is nothing to judge, so OCR it and be sure.

    A low real-word rate means the font's character map is broken: the letters
    look ordinary but form no words. Single letters are excluded, because
    garbage produces them by the hundred and they drown the signal.

    Too few words per line means the extractor emitted one word per line. Every
    word is correct and every line is useless, because a rule matches the start
    of a line and no line is a title any more.

    Note that all three read the whole document. A document whose body is clean
    but whose heading is corrupt passes every check and then matches no rule at
    all; that case is handled by the caller retrying with force_ocr.
    """
    words = [word for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 1]

    if len(words) < MIN_WORDS_TO_JUDGE:
        logger.info(
            "Text layer has only %s words, too few to judge, sending to OCR", len(words)
        )
        return False

    rate = sum(1 for word in words if word in COMMON_WORDS) / len(words)

    if rate < MIN_COMMON_WORD_RATE:
        logger.info(
            "Text layer looks corrupt (%.1f%% of %s words are real words), sending to OCR",
            rate * 100,
            len(words),
        )
        return False

    line_count = sum(1 for line in text.splitlines() if line.strip())
    words_per_line = len(words) / max(line_count, 1)

    if words_per_line < MIN_WORDS_PER_LINE:
        logger.info(
            "Text layer averages %.2f words per line over %s lines, so the line "
            "structure is lost, sending to OCR",
            words_per_line,
            line_count,
        )
        return False

    return True


def _ocr(pdf_bytes: bytes, filename: str, page_limit: int | None = None) -> str:
    """Read a scanned PDF by rendering its pages and running Tesseract on them.

    Slower than reading a text layer, roughly five to ten seconds per page.
    Only reached when a document has no usable text layer.

    Args:
        pdf_bytes: Raw PDF file content.
        filename: Attachment name, for logging and error messages.
        page_limit: Stop after this many pages. None reads the whole document.
            Used by the forced retry, which is only ever looking for a heading.

    Returns:
        The OCR'd text, pages joined by newlines.

    Raises:
        DocumentProblem: If the PDF cannot be rendered or OCR produces nothing.
        SystemProblem: If Tesseract is missing.
    """
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as error:
        raise DocumentProblem(
            f"{filename}: could not open for OCR: {type(error).__name__}: {error}"
        ) from error

    pages = []

    with document:
        for number, page in enumerate(document, start=1):
            if page_limit is not None and number > page_limit:
                break

            try:
                image = _page_image(page.get_pixmap(dpi=OCR_DPI), filename, number)
                pages.append(pytesseract.image_to_string(image, config=TESSERACT_CONFIG))
            except pytesseract.TesseractNotFoundError as error:
                # An environment failure, not a bad document. Stop DALYN rather
                # than quietly sending every scan to Manual Review.
                raise SystemProblem(
                    "Tesseract is not installed or not on PATH. See configure_tesseract."
                ) from error
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


def _page_image(pixmap, filename: str, page_number: int):
    """Turn a rendered page into an image array, with underlines painted out.

    Underlined headings are the norm in these filings, and on a scan the rule
    merges into the baseline of every letter above it. See the comment on
    UNDERLINE_MIN_WIDTH for what that does to the text.

    Falls back to the untouched page if the cleanup fails. Worse OCR is better
    than a lost document.

    Args:
        pixmap: The rendered page from pymupdf.
        filename: For logging only.
        page_number: For logging only.

    Returns:
        An RGB numpy array, which pytesseract accepts directly.
    """
    image = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )

    if pixmap.n == 4:
        image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    elif pixmap.n == 1:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)

    try:
        grey = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        ink = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

        # Opening with a long, one-pixel-tall kernel keeps only the runs of ink
        # that are wider than any letter could be.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (UNDERLINE_MIN_WIDTH, 1))
        rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)

        # A scanned rule is a few pixels tall and slightly skewed, so widen the
        # mask vertically or its edges survive and still touch the glyphs.
        rules = cv2.dilate(
            rules,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, UNDERLINE_THICKEN)),
        )

        if not rules.any():
            return image

        cleaned = image.copy()
        cleaned[rules > 0] = (255, 255, 255)
        return cleaned

    except Exception as error:
        logger.warning(
            "%s: could not strip rules from page %s (%s), OCRing the page as-is",
            filename,
            page_number,
            type(error).__name__,
        )
        return image