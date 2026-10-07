from classifier import TITLE_ONLY_PHRASES, TITLE_REGION_LINES, Rule, classify, compress, normalize
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
        title_only=normalized in TITLE_ONLY_PHRASES,
        row=row,
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
