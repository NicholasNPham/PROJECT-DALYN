from pathlib import Path

from stac import document_label, name_in_text

# Made-up names only.
STAC = "DOE, JOHN A (ALERT)"


def test_name_in_subject_in_portal_order():
    assert name_in_text(STAC, "282026CF000000CFAXMX STATE OF FLORIDA VS DOE, JOHN - Notice")


def test_name_in_first_last_order_with_middle_name():
    assert name_in_text(STAC, "State of Florida v. John Allen Doe, Defendant")


def test_middle_initial_not_required():
    assert name_in_text("DOE, JOHN ALLEN", "Defendant: JOHN DOE")


def test_one_letter_ocr_slip_in_a_long_name_passes():
    assert name_in_text("MCALLISTERSON, JONATHAN", "JONATHAN MCALISTERSON")


def test_one_letter_slip_in_a_short_name_fails_at_90():
    # DOE vs DOW is 0.67. Strict on purpose: Manual Review, not a guess.
    assert not name_in_text(STAC, "STATE VS DOW, JOHN")


def test_surname_alone_is_not_enough():
    assert not name_in_text(STAC, "Notice in the matter of DOE")


def test_parts_of_the_name_far_apart_do_not_match():
    text = "Officer Doe responded to the call and spoke with the victim at length. John was present."
    assert not name_in_text(STAC, text)


def test_a_different_first_name_does_not_match():
    assert not name_in_text(STAC, "STATE VS DOE, JANE")


def test_empty_text_or_one_word_stac_name_fails():
    assert not name_in_text(STAC, "")
    assert not name_in_text("DOE", "JOHN DOE")


def test_multi_word_surname_needs_every_part():
    assert name_in_text("DE LA ROSA, MARIA", "Maria De La Rosa")
    assert not name_in_text("DE LA ROSA, MARIA", "Maria Rosa")


def test_document_label_uses_numbers_not_the_filename() -> None:
    """The filename can carry the defendant's name, so only the numbers are logged."""
    label = document_label(Path("C:/temp/stac_x/0003_2_Doe John Notice.pdf"))

    assert label == "email 3 attachment 2"
    assert "Doe" not in label


def test_document_label_falls_back_without_leaking() -> None:
    """A file not named by main.py is never logged by name."""
    assert document_label(Path("Doe John Notice.pdf")) == "a document"
    assert document_label(Path("abc_1_Doe.pdf")) == "a document"
