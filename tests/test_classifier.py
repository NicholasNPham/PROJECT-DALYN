from classifier import score_document
from models import ClassificationResult


def test_single_winner_returns_correct_result():
    phrase_dict = {
        ("DISCOVERY", "DEMAND"): ["this is the demand discovery", "this is the another demand discovery phrase"],
        ("DISCOVERY", "911AUDIO"): ["911 audio script"],
    }
    whole_pdf_text = "word word word word word this is the demand discovery word word word this is the another demand discovery phrase word word word word 911 audio script"

    assert score_document(phrase_dict, whole_pdf_text) == ClassificationResult("DISCOVERY", "DEMAND", 2, ["this is the demand discovery", "this is the another demand discovery phrase"])

def test_empty_phrase_dict_returns_empty_result():
    phrase_dict = {}
    whole_pdf_text = "word word word word word this is the demand discovery word word word this is the another demand discovery phrase word word word word 911 audio script"

    assert score_document(phrase_dict, whole_pdf_text) == ClassificationResult()


def test_two_winners_phrase_dict_returns_empty_result():
    phrase_dict = {
        ("DISCOVERY", "DEMAND"): ["this is the demand discovery", "this is the another demand discovery phrase"],
        ("DISCOVERY", "911AUDIO"): ["911 audio script", "found another phrase"],
    }
    whole_pdf_text = "word word word word word this is the demand discovery word word word this is the another demand discovery phrase word word word word 911 audio script word word word word word found another phrase"

    assert score_document(phrase_dict, whole_pdf_text) == ClassificationResult()

def test_empty_pdf_text_returns_empty_result():
    phrase_dict = {
        ("DISCOVERY", "DEMAND"): ["this is the demand discovery", "this is the another demand discovery phrase"],
        ("DISCOVERY", "911AUDIO"): ["911 audio script"],
    }
    whole_pdf_text = ""

    assert score_document(phrase_dict, whole_pdf_text) == ClassificationResult()

