from stac import name_in_text

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
