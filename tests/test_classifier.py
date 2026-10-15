from classifier import (
    TITLE_CONTAINS_PHRASES,
    TITLE_ONLY_PHRASES,
    TITLE_REGION_LINES,
    Rule,
    _name_gap_pattern,
    classify,
    compress,
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


def test_no_match_and_empty_text_return_empty_result() -> None:
    """Nothing matched means an empty result, which routes to PLS RVW or review."""
    rules = [_rule("DEMAND FOR DISCOVERY", "DISCOVERY", "DEMAND", 2)]

    assert classify(rules, "Something else entirely") == ClassificationResult()
    assert classify(rules, "") == ClassificationResult()
    assert classify([], "Demand for Discovery") == ClassificationResult()
