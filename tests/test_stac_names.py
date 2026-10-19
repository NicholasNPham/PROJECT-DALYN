from pathlib import Path

import pytest

from stac import document_label, name_in_text, names_match

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


# names_match: the Polk caption check. Made-up names only.


@pytest.mark.parametrize(
    "caption",
    [
        "JOHN A DOE",
        "JOHN A. DOE",
        "JOHN ALLEN DOE",
        "JOHN DOE",
        "DOE, JOHN A",
        "JOHN A DOE MOTION",
        "JOHN A DOE IN AND FOR",
        "JOHN A DOE ORDER ON MOTION TO CONTINUE",
    ],
)
def test_whole_middle_name_in_stac_matches_initial_or_none_in_caption(caption: str) -> None:
    """STAC's full middle name is optional: initial, whole or absent, extra caption words or not."""
    assert names_match("DOE, JOHN ALLEN", caption)


def test_stac_flags_are_ignored() -> None:
    """Alert flags in brackets are not part of the name."""
    assert names_match("DOE, JOHN ALLEN (ALERT)", "JOHN A DOE MOTION")


def test_ocr_slip_in_surname_still_matches_with_extra_words() -> None:
    """The 82% leeway still applies to the required words."""
    assert names_match("THOMPSON, JOHN ALLEN", "JOHN A THORNPSON MOTION")


@pytest.mark.parametrize(
    "caption",
    [
        "JANE A DOE MOTION",
        "JOHN A SMITH MOTION",
        "JOHN A DOW",
    ],
    ids=["different first name", "different surname", "surname too far off"],
)
def test_different_person_still_fails(caption: str) -> None:
    """Loosening the middle name does not loosen the surname or first name."""
    assert not names_match("DOE, JOHN ALLEN", caption)


def test_multi_word_surname_needs_every_part_in_the_caption() -> None:
    """Every word of the surname is required, as in name_in_text."""
    assert names_match("DE LA ROSA, MARIA ELENA", "MARIA E DE LA ROSA MOTION")
    assert not names_match("DE LA ROSA, MARIA ELENA", "MARIA E ROSA MOTION")


# Two-part surnames. STAC writes them hyphenated; filings often print one part.
TWO_PART = "SURA-SURB, FIRST"
CHARGE_RUN_ON = "FIRST SURA DUI DAMAGE TO PERSON OR PROPERTY DMG PROP OFFENSE ON"


def test_first_part_of_two_part_surname_matches_when_caption_runs_on():
    """The 14 Oct 2026 felony filing: one surname part, then the charge text."""
    assert names_match(TWO_PART, CHARGE_RUN_ON)


def test_second_part_of_two_part_surname_matches():
    assert names_match(TWO_PART, "FIRST SURB DUI DAMAGE TO PROPERTY")


def test_two_part_surname_with_a_different_first_name_fails():
    assert not names_match(TWO_PART, "OTHER SURA DUI DAMAGE TO PROPERTY")


def test_two_part_surname_with_neither_part_fails():
    assert not names_match(TWO_PART, "FIRST ELSEWHERE DUI DAMAGE TO PROPERTY")


def test_a_lone_particle_does_not_stand_in_for_the_surname():
    assert not names_match("DE-LA-CRUZ, FIRST", "FIRST DE DUI DAMAGE TO PROPERTY")


def test_spaced_multi_word_surname_still_needs_every_part():
    """Only hyphenated surnames get the one-part rule."""
    assert not names_match("DE LA ROSA, FIRST", "FIRST ROSA DUI DAMAGE TO PROPERTY")


def test_one_part_surname_is_unchanged_by_the_two_part_rule():
    assert not names_match("DOE, JOHN", "JOHN SMITH DUI DAMAGE TO PROPERTY")


def test_highlands_check_finds_one_part_of_a_two_part_surname():
    assert name_in_text(TWO_PART, "282026CF000000CFAXMX STATE OF FLORIDA VS FIRST SURB - Notice")


def test_highlands_check_still_needs_the_first_name_near_the_surname_part():
    assert not name_in_text(TWO_PART, "SURA filed this. Many lines later the word FIRST appears again here once more")


# Initials either way, suffixes optional, conflicts caught. Made-up names only.


@pytest.mark.parametrize(
    "caption",
    [
        "JOHN A. DOE NOTICE OF PROPOSED ORDER ON MOTION TO CONTINUE",
        "JOHN A. DOE, JR.",
        "JOHN ALLEN DOE",
        "J. ALLEN DOE JR",
        "J. A. DOE MOTION",
        "DOE JR, JOHN A",
        "DOE, JOHN MOTION FOR",
    ],
)
def test_suffix_on_stac_surname_is_not_required(caption: str) -> None:
    """STAC writes DOE JR; a caption with or without the JR, or with initials, still matches."""
    assert names_match("DOE JR, JOHN ALLEN", caption)


@pytest.mark.parametrize(
    "caption",
    [
        "JOHN B. DOE JR",
        "JOHN BRIAN DOE MOTION",
        "DOE, JOHN B MOTION",
        "JOHN A DOE SR",
    ],
    ids=["other middle initial", "other middle name", "other initial, surname first", "other suffix"],
)
def test_conflicting_middle_name_or_suffix_fails(caption: str) -> None:
    """Missing is fine, different is not: a different middle or JR against SR is another person."""
    assert not names_match("DOE JR, JOHN ALLEN", caption)


def test_stac_initial_matches_a_whole_name_in_the_caption() -> None:
    """Initials work the other way too: STAC's J and A against the caption's JOHN ALLEN."""
    assert names_match("DOE, J A", "JOHN ALLEN DOE MOTION")
    assert not names_match("DOE, J A", "JOHN BRIAN DOE MOTION")


def test_highlands_check_does_not_require_the_suffix() -> None:
    """The subject and body search drops JR from the required words too."""
    assert name_in_text("DOE JR, JOHN ALLEN", "SERVICE DOE JOHN A")


# Highlands and Hardee read names the way the Polk caption check does.


@pytest.mark.parametrize(
    "text",
    [
        "STATE OF FLORIDA VS J. DOE - Notice",
        "STATE VS DOE, J - Notice",
        "STATE OF FLORIDA V. JOHN A. DOE, Defendant",
    ],
)
def test_highlands_check_accepts_an_initial_for_the_first_name(text: str) -> None:
    """J DOE finds STAC's DOE, JOHN ALLEN, as it already did for Polk."""
    assert name_in_text("DOE, JOHN ALLEN", text)


def test_highlands_check_finds_a_stac_name_with_only_an_initial() -> None:
    """STAC's DOE, J used to come down to DOE alone and could never be found."""
    assert name_in_text("DOE, J", "STATE OF FLORIDA VS JOHN DOE - Notice")
    assert not name_in_text("DOE, J", "STATE OF FLORIDA VS MARK DOE - Notice")


@pytest.mark.parametrize(
    "text",
    [
        "STATE OF FLORIDA VS JOHN B. DOE - Notice",
        "STATE VS DOE, JOHN B - Notice",
        "STATE OF FLORIDA VS JOHN A DOE SR - Notice",
    ],
    ids=["other middle initial", "other initial, surname first", "other suffix"],
)
def test_highlands_check_fails_a_conflicting_middle_name_or_suffix(text: str) -> None:
    """Missing is fine, different is not, the same as Polk."""
    assert not name_in_text("DOE JR, JOHN ALLEN", text)


def test_highlands_check_still_passes_without_middle_name_or_suffix() -> None:
    """Leaving out the middle name and the JR is not a conflict."""
    assert name_in_text("DOE JR, JOHN ALLEN", "STATE OF FLORIDA VS JOHN DOE - Notice")


def test_highlands_check_keeps_its_stricter_bar() -> None:
    """THORNPSON for THOMPSON (0.82) passes the Polk caption check but not the 90% bar."""
    assert names_match("THOMPSON, JOHN", "JOHN THORNPSON")
    assert not name_in_text("THOMPSON, JOHN", "STATE OF FLORIDA VS JOHN THORNPSON")
