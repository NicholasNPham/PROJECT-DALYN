"""Tests for main._upload_name: attachment names made safe to write on Windows.

Motivated by a live run where an order's filename held the whole ruling and
the temp path reached 270 characters, past Windows' 260 limit. No files are
written; only the returned name is checked.
"""

from pathlib import Path

import pytest

import main
from exceptions import SystemProblem

PREFIX = "0004_1_"
# Made up, but as long as the real one: a title that spells out the ruling.
LONG_TITLE = (
    "Proposed Order Granting Motion To Continue Hearing And Extend All Pending "
    "Deadlines (Unopposed As To Form Of The Order - Granted By The Court After "
    "A Hearing Held On The Record With Both Parties Present And Heard)"
)


def _full_length(work_dir: Path, name: str) -> int:
    """Length of the path the file would land at, as Windows counts it."""
    return len(str(work_dir.resolve() / name))


def test_long_name_is_cut_to_fit_under_the_limit(tmp_path: Path) -> None:
    """The crash case: a long title is cut so the whole path fits."""
    work_dir = tmp_path / "stac_20261110_105924"
    name = main._upload_name(f"{LONG_TITLE}.pdf", PREFIX, work_dir)

    assert _full_length(work_dir, name) <= main.MAX_UPLOAD_PATH - main.UPLOAD_PATH_MARGIN
    assert name.startswith(f"{PREFIX}Proposed Order Granting")
    assert name.endswith(".pdf")


def test_long_name_in_a_deep_folder_still_fits(tmp_path: Path) -> None:
    """A deeper install folder gets a shorter name, not a crash."""
    work_dir = tmp_path / ("deep" * 20) / "stac_20261110_105924"
    name = main._upload_name(f"{LONG_TITLE}.pdf", PREFIX, work_dir)

    assert _full_length(work_dir, name) <= main.MAX_UPLOAD_PATH - main.UPLOAD_PATH_MARGIN
    assert name.endswith(".pdf")


def test_short_name_is_left_alone(tmp_path: Path) -> None:
    """A normal filename comes back unchanged apart from the prefix."""
    assert main._upload_name("Order.pdf", PREFIX, tmp_path) == f"{PREFIX}Order.pdf"


def test_surrounding_whitespace_is_stripped(tmp_path: Path) -> None:
    """Matches the old behavior, which stripped the name before writing it."""
    assert main._upload_name("  Order.pdf  ", PREFIX, tmp_path) == f"{PREFIX}Order.pdf"


def test_characters_windows_refuses_become_underscores(tmp_path: Path) -> None:
    """Every character Windows bans in a filename is swapped out."""
    name = main._upload_name('Order: a<b>c"d/e\\f|g?h*i.pdf', PREFIX, tmp_path)
    assert name == f"{PREFIX}Order_ a_b_c_d_e_f_g_h_i.pdf"


def test_control_characters_become_underscores(tmp_path: Path) -> None:
    """A tab inside the name is not allowed in a Windows filename either."""
    assert main._upload_name("Order\tFiled.pdf", PREFIX, tmp_path) == f"{PREFIX}Order_Filed.pdf"


def test_trailing_dots_and_spaces_before_the_extension_are_dropped(tmp_path: Path) -> None:
    """Windows strips these silently, so the name on disk would not match."""
    assert main._upload_name("Order. .pdf", PREFIX, tmp_path) == f"{PREFIX}Order.pdf"


def test_dot_inside_a_title_is_not_taken_as_the_extension(tmp_path: Path) -> None:
    """With no real extension, text after a dot in the title stays part of the title."""
    title = "Order Re. Motion To Continue Hearing"
    assert main._upload_name(title, PREFIX, tmp_path) == f"{PREFIX}{title}"


def test_folder_too_deep_to_fit_any_name_stops_the_run(tmp_path: Path) -> None:
    """Past a point the cut name would be meaningless, so it is a setup problem."""
    work_dir = tmp_path / ("d" * 240)
    with pytest.raises(SystemProblem):
        main._upload_name(f"{LONG_TITLE}.pdf", PREFIX, work_dir)
