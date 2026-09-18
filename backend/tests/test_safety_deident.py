"""Taking the people out of the text, and refusing to guess which words are
people.

Two failures are possible here and they are opposite. Leaking an identifier
puts a patient's name in a vector store and, eventually, in a document. Masking
too eagerly destroys the clinical fact the case exists to record -- "Severe
Headache" is two capitalised words and so is "Jane Smith".

So the module masks what it is sure of, queues what it is not, and the queue
blocks. What is asserted here is mostly the second half: the things it declines
to decide.
"""

import pytest

from app.safety import deident
from app.safety.deident import (
    ADDRESS, DATE_OF_BIRTH, EMAIL, NATIONAL_ID, PATIENT_NAME, PHONE,
    REPORTER_NAME, SITE_NAME, detect, mask, scan,
)

NARRATIVE = (
    "A 34-year-old woman, Jane Smith, developed a severe headache two hours "
    "after the second dose. She was seen by Dr Alan Reed at St Mary's Hospital "
    "and admitted overnight. Contact: alan.reed@stmarys.example on "
    "+44 20 7946 0958. NHS number 943 476 5919. She lives at "
    "12 Marlborough Road, SW1A 1AA. Date of birth 04/03/1992. The Adverse Event "
    "resolved without sequelae."
)


# ------------------------------------------------------- the certain things

@pytest.mark.parametrize("kind,fragment", [
    (EMAIL, "alan.reed@stmarys.example"),
    (PHONE, "20 7946 0958"),
    (REPORTER_NAME, "Alan Reed"),
    (SITE_NAME, "St Mary's Hospital"),
    (NATIONAL_ID, "943 476 5919"),
    (ADDRESS, "12 Marlborough Road"),
    (DATE_OF_BIRTH, "04/03/1992"),
])
def test_the_unambiguous_shapes_are_found(kind, fragment):
    found = {(d.identifier_type, d.text.strip()) for d in detect(NARRATIVE)
             if d.certain}
    assert any(k == kind and fragment in text for k, text in found), found


def test_a_postcode_is_an_address():
    hits = [d for d in detect("Lives at SW1A 1AA.") if d.certain]
    assert any(d.identifier_type == ADDRESS and "SW1A 1AA" in d.text for d in hits)


def test_the_certain_ones_are_masked_without_asking():
    result = mask(NARRATIVE, salt="p1")
    assert "alan.reed@stmarys.example" not in result.masked_text
    assert "St Mary's Hospital" not in result.masked_text
    assert "Alan Reed" not in result.masked_text
    assert "943 476 5919" not in result.masked_text
    assert "04/03/1992" not in result.masked_text


def test_the_masked_text_still_reads_as_a_sentence():
    """Typed tokens rather than a single blackout: a reviewer who cannot follow
    the story cannot check it."""
    result = mask(NARRATIVE, salt="p1")
    assert "[REPORTER-" in result.masked_text
    assert "[SITE-" in result.masked_text
    assert "[EMAIL-" in result.masked_text
    assert "severe headache" in result.masked_text
    assert "admitted overnight" in result.masked_text


# ------------------------------------------------- what it refuses to decide

def test_a_bare_capitalised_pair_is_queued_not_masked():
    """"Jane Smith" and "Severe Headache" are the same shape. Guessing either
    way is silent: one leaks a name, the other destroys a diagnosis."""
    result = mask("The patient Jane Smith was unwell.", salt="p1")
    assert "Jane Smith" in result.masked_text, "not masked on a guess"
    assert [d.text for d in result.queued] == ["Jane Smith"]


def test_a_medical_phrase_is_not_even_queued():
    """A queue that asks whether "Adverse Event" is a patient is a queue people
    clear without reading."""
    queued = {d.text for d in mask(NARRATIVE, salt="p1").queued}
    assert "Adverse Event" not in queued
    assert "Severe Headache" not in queued


@pytest.mark.parametrize("phrase", [
    "Myocardial Infarction", "Preferred Term", "System Organ Class",
    "United Kingdom", "Medical History", "Data Lock Point",
])
def test_known_non_names_are_never_queued(phrase):
    assert not mask(f"Reported as {phrase} today.", salt="p1").queued


def test_a_bare_date_is_not_a_date_of_birth():
    """A date in a narrative is far more often an onset or a visit. Masking one
    destroys the clinical fact the case exists for."""
    result = mask("Symptoms began on 04/03/2026 and resolved by 10/03/2026.",
                  salt="p1")
    assert "04/03/2026" in result.masked_text
    assert "10/03/2026" in result.masked_text


def test_a_labelled_date_of_birth_is_masked():
    result = mask("Date of birth 04/03/1992.", salt="p1")
    assert "04/03/1992" not in result.masked_text
    assert "[DOB-" in result.masked_text


# --------------------------------------------------- what the case already knew

def test_a_value_the_case_carries_is_masked_with_certainty():
    """No pattern is as reliable as a name we were given in advance."""
    text = "Reported by Alberto Nunez, who also supplied the follow-up."
    plain = mask(text, salt="p1")
    assert "Alberto Nunez" in plain.masked_text, "queued without the structured hint"

    known = {REPORTER_NAME: ["Alberto Nunez"]}
    informed = mask(text, known=known, salt="p1")
    assert "Alberto Nunez" not in informed.masked_text
    assert informed.queued == []


def test_a_known_value_wins_over_a_weaker_overlapping_guess():
    known = {PATIENT_NAME: ["Jane Smith"]}
    result = mask("The patient Jane Smith was unwell.", known=known, salt="p1")
    assert "Jane Smith" not in result.masked_text
    assert "[PATIENT-" in result.masked_text
    assert result.queued == []


def test_a_known_value_is_matched_case_insensitively():
    known = {REPORTER_NAME: ["alan reed"]}
    assert "Alan Reed" not in mask("Seen by Alan Reed.", known=known,
                                   salt="p1").masked_text


# ------------------------------------------------------------- the tokens

def test_the_same_value_becomes_the_same_token():
    """A narrative still reads as one story, and two cases can be seen to
    involve the same reporter."""
    result = mask("Dr Alan Reed called. Later Dr Alan Reed wrote.", salt="p1")
    tokens = {piece for piece in result.masked_text.split() if piece.startswith("[")}
    assert len(tokens) == 1


def test_two_products_produce_different_tokens_for_one_name():
    """Salted per product, so a token from one customer's data cannot be used
    to look for the same person in another's."""
    one = mask("Dr Alan Reed called.", salt="product-a").masked_text
    two = mask("Dr Alan Reed called.", salt="product-b").masked_text
    assert one != two


def test_a_token_does_not_contain_the_value_it_replaced():
    result = mask("Dr Alan Reed called.", salt="p1")
    token = next(iter(result.replacements.values()))
    assert "reed" not in token.lower() and "alan" not in token.lower()


def test_the_replacements_are_the_decision_log():
    """§6 asks for every masking decision to be logged. This is what the caller
    writes to the audit trail."""
    result = mask(NARRATIVE, salt="p1")
    assert "Alan Reed" in result.replacements
    assert result.replacements["Alan Reed"].startswith("[REPORTER-")


# ------------------------------------------------------ a reviewer's answers

def test_a_confirmed_candidate_is_masked_on_the_next_pass():
    text = "The patient Jane Smith was unwell."
    result = mask(text, salt="p1", accept={"Jane Smith": PATIENT_NAME})
    assert "Jane Smith" not in result.masked_text
    assert "[PATIENT-" in result.masked_text
    assert result.queued == []


def test_a_rejected_candidate_is_left_alone_and_not_asked_again():
    """Somebody who has said a phrase is a diagnosis should not be asked about
    it on every later source."""
    text = "Diagnosis of Brugada Syndrome confirmed."
    result = mask(text, salt="p1", accept={"Brugada Syndrome": None})
    assert "Brugada Syndrome" in result.masked_text
    assert result.queued == []


def test_an_unanswered_candidate_keeps_blocking():
    result = mask("The patient Jane Smith and also Peter Jones.", salt="p1",
                  accept={"Jane Smith": PATIENT_NAME})
    assert [d.text for d in result.queued] == ["Peter Jones"]


# ------------------------------------------------------------ the leak scan

def test_the_scan_finds_what_survived_masking():
    """§11's first blocker, run over text that is supposed to be clean."""
    leaked = "Reported by Dr Alan Reed at alan@example.com."
    assert scan(leaked)
    assert not scan(mask(leaked, salt="p1").masked_text)


def test_the_scan_ignores_the_uncertain_ones():
    """A scan that fired on every capitalised pair would flag "Preferred Term"
    in every document and teach people to ignore it."""
    assert scan("The patient Jane Smith was unwell.") == []


def test_the_scan_passes_clean_text():
    assert scan("The subject developed a headache after the second dose.") == []


def test_empty_text_is_not_an_error():
    assert detect("") == []
    assert mask("", salt="p1").masked_text == ""
    assert scan("") == []


def test_every_identifier_type_has_a_token_prefix():
    """A type with no prefix would mask to `[REDACTED-...]` and lose the
    distinction the typed tokens exist for."""
    for kind in deident.IDENTIFIER_TYPES:
        assert kind in deident._TOKEN_PREFIX


# ---------------------------------------------------- the overlap resolution

def test_a_longer_certain_detection_displaces_a_shorter_one():
    """Two patterns can claim overlapping characters. The longer, more
    confident one wins -- a partial mask that leaves half a name is worse than
    none, because it looks done."""
    known = {REPORTER_NAME: ["Alan Reed Fitzgerald"]}
    result = mask("Seen by Dr Alan Reed Fitzgerald today.", known=known, salt="p1")
    assert "Alan Reed" not in result.masked_text
    assert "Fitzgerald" not in result.masked_text


def test_a_known_value_shorter_than_three_characters_is_ignored():
    """Matching a two-letter value literally would black out every occurrence
    of those letters in the narrative."""
    result = mask("The patient took it as directed.", known={PATIENT_NAME: ["it"]},
                  salt="p1")
    assert result.masked_text == "The patient took it as directed."


def test_a_phrase_made_only_of_stoplist_words_is_not_queued():
    assert not mask("Recorded as Case Number today.", salt="p1").queued


def test_overlapping_applied_detections_do_not_double_mask():
    """Two accepted answers covering the same characters produce one token,
    not a token inside a token."""
    result = mask("Seen by Dr Alan Reed.", salt="p1",
                  accept={"Alan Reed": REPORTER_NAME, "Alan": PATIENT_NAME})
    assert result.masked_text.count("[") == 1


# ------------------------------------------------- what the case already knows

def test_known_values_reads_the_case_identifiers():
    from types import SimpleNamespace

    case = SimpleNamespace(worldwide_case_id="GB-ACME-2026001",
                           local_case_ids=["ACME-LOCAL-77"])
    known = deident.known_values(case, reporter_fields=["Alan Reed"])
    assert "GB-ACME-2026001" in known[deident.PATIENT_ID]
    assert "ACME-LOCAL-77" in known[deident.PATIENT_ID]
    assert known[REPORTER_NAME] == ["Alan Reed"]


def test_known_values_drops_the_types_it_found_nothing_for():
    from types import SimpleNamespace

    case = SimpleNamespace(worldwide_case_id=None, local_case_ids=[])
    assert deident.known_values(case) == {}


def test_a_case_identifier_in_the_narrative_is_masked():
    from types import SimpleNamespace

    case = SimpleNamespace(worldwide_case_id="GB-ACME-2026001", local_case_ids=[])
    known = deident.known_values(case)
    result = mask("Refer to case GB-ACME-2026001 for the follow-up.",
                  known=known, salt="p1")
    assert "GB-ACME-2026001" not in result.masked_text
    assert "[PATIENT-ID-" in result.masked_text
