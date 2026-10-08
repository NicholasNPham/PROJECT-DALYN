import ucn


def test_polk_dashed_form_still_matches():
    assert ucn.find_all("Case 53-2026-CF-000000-A000-XX filed") == ["532026CF000000A000XX"]


def test_highlands_felony_long_form_matches():
    assert ucn.find_all("282026CF000000CFAXMX") == ["282026CF000000CFAXMX"]


def test_hardee_misdemeanor_dashed_long_form_matches():
    assert ucn.find_all("25-2026-MM-000000-MM-AXMX") == ["252026MM000000MMAXMX"]


def test_highlands_traffic_ct_and_tt_are_interchangeable():
    assert ucn.find_all("282026CT000000TTAXMX") == ["282026CT000000TTAXMX"]
    assert ucn.find_all("282026TT000000CTAXMX") == ["282026TT000000CTAXMX"]


def test_hardee_traffic_ct_on_both_sides_matches():
    assert ucn.find_all("252026CT000000CTAXMX") == ["252026CT000000CTAXMX"]


def test_mismatched_court_positions_are_not_a_case_number():
    assert ucn.find_all("282026CF000000MMAXMX") == []


def test_unknown_location_code_is_not_a_case_number():
    assert ucn.find_all("282026CF000000CFAXMY") == []


def test_other_county_in_highlands_shape_is_not_a_case_number():
    assert ucn.find_all("532026CF000000CFAXMX") == []


def test_hardee_number_in_polk_shape_is_not_truncated():
    # Would otherwise come back as ...A000XX, a wrong case number.
    assert ucn.find_all("25-2026-MM-000000-A000-XXW") == []


def test_mixed_counties_come_back_in_order_of_appearance():
    text = "282026CF000000CFAXMX and later 53-2026-CF-000000-A000-XX"
    assert ucn.find_all(text) == ["282026CF000000CFAXMX", "532026CF000000A000XX"]


def test_subject_wins_over_document_for_highlands():
    found = ucn.find_ucn(
        document_text="53-2026-CF-000000-A000-XX",
        subject="Filing in 28-2026-CF-000000-CF-AXMX",
    )
    assert found == "282026CF000000CFAXMX"


HIGHLANDS_CF = ucn.CaseRef("28", "2026", "CF", "000000")
NO_COUNTY_CF = ucn.CaseRef(None, "2026", "CF", "000000")


def test_every_form_in_the_write_up_names_the_same_highlands_case():
    for printed in (
        "CF26-00000AXXS",
        "CF26-00000",
        "2026-CF-000000-XX",
        "28-2026-CF-000000-XX",
        "2026-CF-000000-CFAXMX",
    ):
        refs = ucn.find_case_refs(f"Case No. {printed} Page 1")
        assert refs, printed
        assert all(ucn.refers_to("282026CF000000CFAXMX", r) for r in refs), printed


def test_short_form_division_pins_the_county():
    assert ucn.find_case_refs("CF26-00000AXXS") == [HIGHLANDS_CF]
    assert ucn.find_case_refs("MM26-00000AXXW") == [ucn.CaseRef("25", "2026", "MM", "000000")]
    assert ucn.find_case_refs("CF26-00000") == [NO_COUNTY_CF]


def test_traffic_short_form_keeps_six_digits_and_folds_tt_into_ct():
    assert ucn.find_case_refs("TT26-000000XXS") == [ucn.CaseRef("28", "2026", "CT", "000000")]


def test_short_form_next_to_a_page_number_still_matches():
    assert ucn.find_case_refs("CF26-00000 12") == [NO_COUNTY_CF]


def test_short_form_inside_a_longer_number_does_not_match():
    assert ucn.find_case_refs("CF26000001") == []
    assert ucn.find_case_refs("XCF26-00000") == []


def test_polk_number_gives_no_case_ref():
    assert ucn.find_case_refs("53-2026-CF-000000-A000-XX") == []


def test_other_county_does_not_refer_to_the_email_case():
    hardee = ucn.find_case_refs("CF26-00000AXXW")
    assert not ucn.refers_to("282026CF000000CFAXMX", hardee[0])


def test_different_sequence_or_court_does_not_refer():
    assert not ucn.refers_to("282026CF000000CFAXMX", ucn.CaseRef(None, "2026", "CF", "000001"))
    assert not ucn.refers_to("282026CF000000CFAXMX", ucn.CaseRef(None, "2026", "MM", "000000"))


def test_ct_and_tt_refer_to_the_same_traffic_case():
    ref = ucn.find_case_refs("TT26-000000")[0]
    assert ucn.refers_to("282026CT000000TTAXMX", ref)
    assert ucn.refers_to("282026TT000000CTAXMX", ref)


def test_long_form_rebuilt_per_county():
    assert ucn.to_long_form(HIGHLANDS_CF) == "282026CF000000CFAXMX"
    assert ucn.to_long_form(ucn.CaseRef("28", "2026", "CT", "000000")) == "282026CT000000TTAXMX"
    assert ucn.to_long_form(ucn.CaseRef("25", "2026", "CT", "000000")) == "252026CT000000CTAXMX"


def test_long_form_needs_a_county():
    assert ucn.to_long_form(NO_COUNTY_CF) is None


# --- choose_ucn: the agreed table, fed text the way main.py feeds it -------

HIGHLANDS = "282026CF000000CFAXMX"
POLK = "532026CF000000A000XX"


def _choose(subject: str = "", body: str = "", document: str = ""):
    """Run choose_ucn on raw text, as _process_attachment does."""
    document_ucns = ucn.find_all(document)
    refs = ucn.find_document_refs(document, document_ucns)
    return ucn.choose_ucn(ucn.find_ucn("", subject=subject), ucn.find_all(body), document_ucns, refs)


def test_highlands_email_and_document_short_form_agree():
    assert _choose(subject=HIGHLANDS, document="Case No. CF26-00000AXXS") == ((HIGHLANDS, "subject"), "")


def test_highlands_email_with_no_document_number_uses_email():
    assert _choose(subject=HIGHLANDS, document="Notice of Hearing") == ((HIGHLANDS, "subject"), "")


def test_highlands_email_agrees_when_document_also_cites_other_cases():
    document = "CF26-00000AXXS, see also CF25-00001AXXS"
    assert _choose(subject=HIGHLANDS, document=document) == ((HIGHLANDS, "subject"), "")


def test_highlands_email_with_document_full_long_form_agrees():
    assert _choose(subject=HIGHLANDS, document="28-2026-CF-000000-CF-AXMX") == ((HIGHLANDS, "subject"), "")


def test_highlands_email_with_only_other_cases_in_document_conflicts():
    chosen, conflict = _choose(subject=HIGHLANDS, document="Case No. CF26-00001AXXS")
    assert chosen is None
    assert "282026CF000001CFAXMX" in conflict


def test_highlands_email_with_hardee_short_form_in_document_conflicts():
    chosen, conflict = _choose(subject=HIGHLANDS, document="CF26-00000AXXW")
    assert chosen is None and conflict


def test_highlands_email_with_only_a_polk_citation_conflicts():
    chosen, conflict = _choose(subject=HIGHLANDS, document="53-2026-CF-000000-A000-XX")
    assert chosen is None and conflict


def test_traffic_email_agrees_with_tt_short_form():
    email = "282026CT000000TTAXMX"
    assert _choose(subject=email, document="TT26-000000XXS") == ((email, "subject"), "")


def test_body_ucn_used_when_subject_has_none():
    assert _choose(body=HIGHLANDS, document="CF26-00000") == ((HIGHLANDS, "body"), "")


def test_no_email_one_document_case_with_county_is_rebuilt():
    assert _choose(document="CF26-00000AXXS") == ((HIGHLANDS, "document"), "")


def test_no_email_one_case_printed_several_ways_is_one_case():
    document = "CF26-00000AXXS ... 2026-CF-000000-XX ... CF26-00000"
    assert _choose(document=document) == ((HIGHLANDS, "document"), "")


def test_no_email_document_case_without_county_gives_no_ucn():
    assert _choose(document="Case No. CF26-00000") == (None, "")


def test_no_email_two_document_cases_conflict():
    chosen, conflict = _choose(document="CF26-00000AXXS and CF26-00001AXXS")
    assert chosen is None
    assert "2 cases" in conflict


def test_no_email_county_less_form_of_another_case_conflicts():
    chosen, conflict = _choose(document="CF26-00000AXXS and CF26-00001")
    assert chosen is None and conflict


def test_no_email_same_number_in_both_counties_conflicts():
    chosen, conflict = _choose(document="CF26-00000AXXS and CF26-00000AXXW")
    assert chosen is None and conflict


def test_body_naming_two_cases_with_no_subject_conflicts():
    chosen, conflict = _choose(body=f"{HIGHLANDS} and 282026CF000001CFAXMX")
    assert chosen is None and conflict


# --- Polk is unchanged -----------------------------------------------------

def test_polk_email_and_document_agree():
    assert _choose(subject=POLK, document="53-2026-CF-000000-A000-XX") == ((POLK, "subject"), "")


def test_polk_email_ignores_short_forms_in_document():
    assert _choose(subject=POLK, document="Case No. 2026-CF-000001") == ((POLK, "subject"), "")


def test_polk_email_with_other_polk_number_conflicts():
    chosen, conflict = _choose(subject=POLK, document="53-2026-CF-000001-A000-XX")
    assert chosen is None and conflict


def test_no_email_polk_document_with_county_less_citation_still_uses_polk():
    document = "53-2026-CF-000000-A000-XX, previously 2025-CF-000001"
    assert _choose(document=document) == ((POLK, "document"), "")


def test_case_labels_list_a_long_form_once():
    document = "282026CF000000CFAXMX"
    labels = ucn.case_labels(ucn.find_all(document), ucn.find_document_refs(document, ucn.find_all(document)))
    assert labels == [HIGHLANDS]


# --- document_labels: what the CSV shows ----------------------------------

def _labels(document: str, chosen: str | None):
    document_ucns = ucn.find_all(document)
    return ucn.document_labels(document_ucns, ucn.find_document_refs(document, document_ucns), chosen)


def test_polk_row_hides_county_less_short_forms():
    assert _labels("Case No. 2026-CF-000000, see 2026-CF-000001", POLK) == []


def test_polk_row_keeps_full_numbers():
    assert _labels("53-2026-CF-000000-A000-XX and 2026-CF-000001", POLK) == [POLK]


def test_highlands_row_keeps_county_less_short_forms():
    assert _labels("CF26-00000", HIGHLANDS) == ["2026CF000000 (no county)"]


def test_row_with_no_chosen_case_keeps_everything():
    assert _labels("CF26-00000", None) == ["2026CF000000 (no county)"]


# --- juvenile: CJ first, JL second ---------------------------------------


def test_highlands_juvenile_long_form_matches():
    assert ucn.find_all("282026CJ000000JLAXMX") == ["282026CJ000000JLAXMX"]


def test_hardee_juvenile_dashed_long_form_matches():
    assert ucn.find_all("25-2026-CJ-000000-JL-AXMX") == ["252026CJ000000JLAXMX"]


def test_juvenile_from_the_subject_is_found():
    subject = "282026CJ000000JLAXMX STATE OF FLORIDA VS DOE, JOHN - Notice"
    assert ucn.find_ucn("", subject=subject) == "282026CJ000000JLAXMX"


def test_juvenile_codes_only_pair_with_each_other():
    # CJ repeated, JL first, or JL after another court: none is a case number.
    assert ucn.find_all("282026CJ000000CJAXMX") == []
    assert ucn.find_all("282026CF000000JLAXMX") == []
    assert ucn.find_all("282026JL000000JLAXMX") == []


def test_juvenile_long_form_rebuilds_with_jl():
    ref = ucn.case_ref("282026CJ000000JLAXMX")
    assert ref == ucn.CaseRef("28", "2026", "CJ", "000000")
    assert ucn.to_long_form(ref) == "282026CJ000000JLAXMX"


def test_juvenile_email_with_no_document_number_uses_email():
    juvenile = "282026CJ000000JLAXMX"
    assert _choose(subject=juvenile, document="Notice of Hearing") == ((juvenile, "subject"), "")
