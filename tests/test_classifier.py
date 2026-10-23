import logging
from pathlib import Path

import pytest
from openpyxl import Workbook

from classifier import (
    TITLE_CONTAINS_PHRASES,
    TITLE_ONLY_PHRASES,
    TITLE_REGION_LINES,
    Rule,
    _name_gap_pattern,
    classify,
    compress,
    load_rules,
    normalize,
)
from models import ClassificationResult


def _rule(phrase: str, document_type: str | None, subtype: str | None, row: int, fuzzy: bool = False) -> Rule:
    """Build a Rule the way load_rules does, without needing a spreadsheet."""
    normalized = normalize(phrase)
    return Rule(
        phrase=phrase,
        normalized=normalized,
        compressed=compress(phrase),
        document_type=document_type,
        document_subtype=subtype,
        fuzzy=fuzzy,
        title_only=normalized in TITLE_ONLY_PHRASES or normalized in TITLE_CONTAINS_PHRASES,
        row=row,
        title_contains=normalized in TITLE_CONTAINS_PHRASES,
    )


def test_phrase_starting_a_line_matches() -> None:
    """A rule matches when a line of the document begins with its phrase."""
    rules = [_rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 2)]
    result = classify(rules, "IN THE CIRCUIT COURT\nDemand for Discovery\nThe defendant demands")

    assert result.document_type == "DISCOVERY"
    assert result.document_subtype == "DEMAND"
    assert result.rule_row == 2
    assert result.similarity == 1.0


def test_phrase_in_the_middle_of_a_line_does_not_match() -> None:
    """A motion asking for an order must not be filed as an order."""
    rules = [_rule("ORDER", "COURT", "ORDER", 2)]

    assert classify(rules, "MOTION\nPlease enter an order withdrawing counsel") == ClassificationResult()


def test_first_matching_rule_wins() -> None:
    """Rules are tried top to bottom, so the higher row wins when both match."""
    rules = [
        _rule("NOTICE OF APPEARANCE", "NOTICE", "APPEARANCE", 2),
        _rule("NOTICE", "NOTICE", "GENERAL", 3),
    ]

    assert classify(rules, "Notice of Appearance").rule_row == 2


def test_title_only_rule_ignores_lines_past_the_title_region() -> None:
    """ORDER only looks where a title can be, not at citations deep in the body."""
    rules = [_rule("ORDER", "COURT", "ORDER", 2)]
    body = "\n".join(["filler line"] * TITLE_REGION_LINES + ["Order granting the motion"])

    assert classify(rules, body) == ClassificationResult()


def test_notice_of_participation_matches_after_the_defendants_name() -> None:
    """The defense heading puts the name first, so the phrase is mid-line."""
    rules = [_rule("NOTICE OF PARTICIPATION", "DISCOVERY", "DEPOSITION", 2)]
    text = "STATE OF FLORIDA\nDEFENDANT JOHN DOE NOTICE OF PARTICIPATION AT DEPOSITIONS"

    assert classify(rules, text).rule_row == 2


def test_notice_of_participation_is_ignored_past_the_title_region() -> None:
    """Mid-line matching is for titles only, not a mention deep in the body."""
    rules = [_rule("NOTICE OF PARTICIPATION", "DISCOVERY", "DEPOSITION", 2)]
    body = "\n".join(["filler line"] * TITLE_REGION_LINES + ["see the notice of participation filed earlier"])

    assert classify(rules, body) == ClassificationResult()


def test_notice_of_participation_needs_whole_words() -> None:
    """Part of a longer word does not count as the phrase."""
    rules = [_rule("NOTICE OF PARTICIPATION", "DISCOVERY", "DEPOSITION", 2)]

    assert classify(rules, "DEFENDANT NOTICE OF PARTICIPATIONS") == ClassificationResult()


def _sheet_rules(tmp_path: Path, *rows: tuple) -> list[Rule]:
    """Load made-up rows through load_rules: (phrase, type, subtype[, fuzzy])."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rules"
    sheet.append(["Order", "Phrase", "TYPE", "SUBTYPE", "Fuzzy?"])
    for number, (phrase, document_type, subtype, *fuzzy) in enumerate(rows, start=1):
        sheet.append([number, phrase, document_type, subtype, fuzzy[0] if fuzzy else None])
    path = tmp_path / "rules.xlsx"
    workbook.save(path)
    return load_rules(path)


@pytest.mark.parametrize("phrase", ["* MOTION TO CONTINUE", "* UNOPPOSED MOTION TO CONTINUE"])
@pytest.mark.parametrize(
    "title",
    [
        "NOTICE OF CALENDAR CONFLICT AND UNOPPOSED MOTION TO CONTINUE",
        "NOTICE OF UNAVAILABILITY AND UNOPPOSED MOTION TO CONTINUE",
        "UNOPPOSED MOTION TO CONTINUE",
    ],
)
def test_leading_star_matches_after_anything_in_the_title(tmp_path: Path, phrase: str, title: str) -> None:
    """A * row matches its phrase after a notice of something else, written either way."""
    rules = _sheet_rules(tmp_path, (phrase, "COURT", "MOTIONS"))

    assert classify(rules, f"STATE OF FLORIDA\n{title}").rule_row == 2


def test_leading_star_matches_after_a_law_firm_name(tmp_path: Path) -> None:
    """A firm's name of any length before the phrase, longer than a name gap allows."""
    rules = _sheet_rules(tmp_path, ("* MOTION TO WITHDRAW", "COURT", "MOTIONS"))
    title = "THE DOE AND ROE LAW FIRM S MOTION TO WITHDRAW AS ATTORNEY"

    assert classify(rules, f"STATE OF FLORIDA\n{title}").rule_row == 2


@pytest.mark.parametrize(
    "title",
    [
        "ORDER GRANTING DEFENDANT'S UNOPPOSED MOTION TO CONTINUE",
        "ORDER ON MOTION TO CONTINUE",
        "NOTICE OF HEARING ON MOTION TO CONTINUE",
        "STATE'S RESPONSE TO MOTION TO CONTINUE",
    ],
)
def test_filing_about_the_motion_is_not_the_motion(tmp_path: Path, title: str) -> None:
    """An order, hearing notice or response names the motion mid-line but is not one."""
    rules = _sheet_rules(tmp_path, ("* MOTION TO CONTINUE", "COURT", "MOTIONS"))

    assert classify(rules, f"STATE OF FLORIDA\n{title}") == ClassificationResult()


def test_leading_star_looks_only_in_the_title_region(tmp_path: Path) -> None:
    """A mention deep in the body is not a title."""
    rules = _sheet_rules(tmp_path, ("* MOTION TO WITHDRAW", "COURT", "MOTIONS"))
    body = "\n".join(["filler line"] * TITLE_REGION_LINES + ["counsel filed a motion to withdraw"])

    assert classify(rules, body) == ClassificationResult()


def test_longer_star_row_above_wins(tmp_path: Path) -> None:
    """MOTION TO WITHDRAW PLEA above MOTION TO WITHDRAW keeps a plea motion apart."""
    rules = _sheet_rules(
        tmp_path,
        ("* MOTION TO WITHDRAW PLEA", "COURT", "PLEA"),
        ("* MOTION TO WITHDRAW", "COURT", "MOTIONS"),
    )

    assert classify(rules, "STATE OF FLORIDA\nDEFENDANT S MOTION TO WITHDRAW PLEA").rule_row == 2
    assert classify(rules, "STATE OF FLORIDA\nTHE FIRM S MOTION TO WITHDRAW AS COUNSEL").rule_row == 3


def test_leading_star_row_is_exact_only_and_kept_apart_from_the_plain_row(tmp_path: Path) -> None:
    """Fuzzy is switched off, and * X does not count as a repeat of a plain X row."""
    rules = _sheet_rules(
        tmp_path,
        ("MOTION TO CONTINUE", "COURT", "MOTIONS"),
        ("* MOTION TO CONTINUE", "COURT", "MOTIONS", "YES"),
    )

    assert [rule.row for rule in rules] == [2, 3]
    assert rules[1].title_contains and not rules[1].fuzzy


def _gap_rule(phrase: str, row: int = 2) -> Rule:
    """A * rule, built the way load_rules builds one."""
    return Rule(
        phrase=phrase,
        normalized=phrase.lower(),
        compressed="",
        document_type="DISCOVERY",
        document_subtype="DEFNS DSCL",
        fuzzy=False,
        title_only=True,
        row=row,
        name_gap=_name_gap_pattern(phrase),
    )


def test_name_gap_matches_a_name_between_the_words() -> None:
    """The defense title that started it: the name sits mid-heading."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    assert classify(rules, "STATE OF FLORIDA\nDefendant John Q Doe's Witness List").rule_row == 2


def test_name_gap_matches_with_no_name() -> None:
    """The same title without a name still matches."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    assert classify(rules, "Defendant's Witness List").rule_row == 2
    assert classify(rules, "Defendant Witness List").rule_row == 2


def test_name_gap_will_not_swallow_a_different_document() -> None:
    """A motion about a witness list is a motion, not a witness list."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    assert classify(rules, "Defendant's Motion to Strike State's Witness List") == ClassificationResult()
    assert classify(rules, "Defendant's Objection Witness List") == ClassificationResult()


def test_name_gap_is_at_most_four_words() -> None:
    """A long run of words is a sentence, not a name."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    assert classify(rules, "Defendant one two three four five witness list") == ClassificationResult()


def test_name_gap_looks_only_in_the_title_region() -> None:
    """Like the catch-alls, a * rule is for titles only."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]
    body = "\n".join(["filler line"] * TITLE_REGION_LINES + ["Defendant John Doe's witness list"])

    assert classify(rules, body) == ClassificationResult()


def test_name_gap_needs_words_on_both_sides() -> None:
    """A * at either end, or two of them, is refused."""
    assert _name_gap_pattern("* WITNESS LIST") is None
    assert _name_gap_pattern("DEFENDANT *") is None
    assert _name_gap_pattern("DEFENDANT * WITNESS * LIST") is None


def test_fuzzy_rule_matches_ocr_mangled_heading() -> None:
    """The real batch 3 case: DEMAND FOR DISCOVERY read back as demandfordiscqvery."""
    rules = [_rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 2, fuzzy=True)]
    result = classify(rules, "demandfordiscqvery")

    assert result.document_type == "DISCOVERY"
    assert result.is_fuzzy


def test_safety_net_only_wins_when_nothing_else_matches() -> None:
    """A blank-Type catch-all is tried after every real rule, fuzzy included."""
    rules = [
        _rule("CERTIFICATE OF SERVICE", None, None, 2),
        _rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 3, fuzzy=True),
    ]

    assert classify(rules, "demandfordiscqvery\nCertificate of Service").rule_row == 3
    assert classify(rules, "Certificate of Service").rule_row == 2


def test_title_split_over_two_lines_matches() -> None:
    """A wrapped title used to go to PLS RVW although the sheet had its row."""
    rules = [_rule("MOTION TO WITHDRAW AS COUNSEL", "COURT", "MOTIONS", 2)]

    assert classify(rules, "STATE OF FLORIDA\nMOTION TO WITHDRAW AS\nCOUNSEL\nbody").rule_row == 2


def test_name_gap_title_split_by_a_long_name_matches() -> None:
    """A long name is what usually pushes a defense title onto a second line."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    assert classify(rules, "STATE OF FLORIDA\nDEFENDANT JOHN\nDOE'S WITNESS LIST").rule_row == 2


def test_leading_star_title_split_over_two_lines_matches(tmp_path: Path) -> None:
    """A firm's name, then the phrase broken across the line end."""
    rules = _sheet_rules(tmp_path, ("* MOTION TO WITHDRAW", "COURT", "MOTIONS"))

    assert classify(rules, "STATE OF FLORIDA\nTHE DOE LAW FIRM S MOTION TO\nWITHDRAW AS COUNSEL").rule_row == 2


def test_fuzzy_title_split_over_two_lines_matches() -> None:
    """A wrapped title that OCR also mangled."""
    rules = [_rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 2, fuzzy=True)]
    result = classify(rules, "STATE OF FLORIDA\nDEMAND FOR\nDISCQVERY")

    assert result.rule_row == 2
    assert result.is_fuzzy


def test_joined_lines_never_change_a_single_line_match() -> None:
    """Joining is a last try: a lower row matching one line still wins over a higher row's join."""
    rules = [
        _rule("MOTION TO WITHDRAW AS COUNSEL", "COURT", "COUNSEL", 2),
        _rule("MOTION TO WITHDRAW", "COURT", "MOTIONS", 3),
    ]

    assert classify(rules, "STATE OF FLORIDA\nMOTION TO WITHDRAW AS\nCOUNSEL").rule_row == 3


def test_joined_lines_stay_in_the_title_region() -> None:
    """A title split past the title region is body text and is not joined."""
    rules = [_rule("MOTION TO WITHDRAW AS COUNSEL", "COURT", "MOTIONS", 2)]
    body = "\n".join(["filler line"] * (TITLE_REGION_LINES + 5) + ["MOTION TO WITHDRAW AS", "COUNSEL"])

    assert classify(rules, body) == ClassificationResult()


def test_title_over_three_lines_is_not_joined() -> None:
    """Two lines at most, so a caption and a title are less likely to be glued together."""
    rules = [_rule("MOTION TO WITHDRAW AS COUNSEL", "COURT", "MOTIONS", 2)]

    assert classify(rules, "STATE OF FLORIDA\nMOTION TO\nWITHDRAW AS\nCOUNSEL") == ClassificationResult()


def test_safety_net_still_comes_after_a_joined_title() -> None:
    """A catch-all row loses to a real row found across two lines."""
    rules = [
        _rule("CERTIFICATE OF SERVICE", None, None, 2),
        _rule("MOTION TO WITHDRAW AS COUNSEL", "COURT", "MOTIONS", 3),
    ]

    assert classify(rules, "MOTION TO WITHDRAW AS\nCOUNSEL\nCertificate of Service").rule_row == 3


def test_joined_match_logs_the_row_not_the_title(caplog: pytest.LogCaptureFixture) -> None:
    """The title can carry a defendant's name, so only the sheet's phrase is logged."""
    rules = [_gap_rule("DEFENDANT * WITNESS LIST")]

    with caplog.at_level(logging.INFO, logger="dalyn.classifier"):
        classify(rules, "STATE OF FLORIDA\nDEFENDANT JOHN\nDOE'S WITNESS LIST")

    assert "across two title lines" in caplog.text
    assert "DEFENDANT * WITNESS LIST" in caplog.text
    assert "john" not in caplog.text.lower()


def test_no_match_and_empty_text_return_empty_result() -> None:
    """Nothing matched means an empty result, which routes to PLS RVW or review."""
    rules = [_rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 2)]

    assert classify(rules, "Something else entirely") == ClassificationResult()
    assert classify(rules, "") == ClassificationResult()
    assert classify([], "Demand for Discovery") == ClassificationResult()
