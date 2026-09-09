"""Reading an ICSR without inventing anything.

What is asserted throughout is the difference between a fact and a silence. An
E2B file that does not state seriousness has not stated that the case is not
serious, and a parser that fills the column with False has made a regulatory
determination and attributed it to whoever imported the file.
"""

from datetime import date

import pytest

from app.safety.e2b import E2bUnreadable, parse_date, parse_icsr

R2 = """<?xml version="1.0" encoding="UTF-8"?>
<ichicsr lang="en">
  <safetyreport>
    <safetyreportid>GB-ACME-2026001</safetyreportid>
    <companynumb>ACME-LOCAL-77</companynumb>
    <safetyreportversion>2</safetyreportversion>
    <occurcountry>GB</occurcountry>
    <reporttype>1</reporttype>
    <serious>1</serious>
    <seriousnesshospitalization>1</seriousnesshospitalization>
    <seriousnessdeath>2</seriousnessdeath>
    <receivedate>20260204</receivedate>
    <receiptdate>20260318</receiptdate>
    <primarysource>
      <qualification>1</qualification>
      <medicalconfirm>1</medicalconfirm>
    </primarysource>
    <patient>
      <patientonsetage>34</patientonsetage>
      <patientonsetageunit>801</patientonsetageunit>
      <patientsex>2</patientsex>
      <reaction>
        <primarysourcereaction>severe headache after second dose</primarysourcereaction>
        <reactionmeddrallt>Headache severe</reactionmeddrallt>
        <reactionmeddrapt>Headache</reactionmeddrapt>
        <reactionmeddraversionpt>27.0</reactionmeddraversionpt>
        <reactionstartdate>20260201</reactionstartdate>
        <reactionoutcome>1</reactionoutcome>
      </reaction>
      <reaction>
        <primarysourcereaction>felt sick</primarysourcereaction>
        <reactionmeddrapt>Nausea</reactionmeddrapt>
        <reactionoutcome>2</reactionoutcome>
      </reaction>
      <drug>
        <drugcharacterization>1</drugcharacterization>
        <medicinalproduct>VIGILAZINE 10 MG TABLETS</medicinalproduct>
        <drugdosagetext>10 mg once daily</drugdosagetext>
        <drugadministrationroute>048</drugadministrationroute>
        <drugindication>Anxiety</drugindication>
        <drugstartdate>20260115</drugstartdate>
        <actiondrug>1</actiondrug>
      </drug>
      <drug>
        <drugcharacterization>2</drugcharacterization>
        <medicinalproduct>Paracetamol</medicinalproduct>
      </drug>
      <summary>
        <narrativeincludeclinical>A 34-year-old woman, Jane Smith, reported a severe
        headache. Reported by Dr Alan Reed at St Mary's Hospital.</narrativeincludeclinical>
      </summary>
    </patient>
  </safetyreport>
</ichicsr>
"""

MINIMAL = """<?xml version="1.0"?>
<ichicsr>
  <safetyreport>
    <safetyreportid>MIN-1</safetyreportid>
    <patient>
      <reaction><primarysourcereaction>rash</primarysourcereaction></reaction>
    </patient>
  </safetyreport>
</ichicsr>
"""

TWO_REPORTS = """<?xml version="1.0"?>
<ichicsr>
  <safetyreport><safetyreportid>A-1</safetyreportid>
    <patient><reaction><reactionmeddrapt>Headache</reactionmeddrapt></reaction></patient>
  </safetyreport>
  <safetyreport><safetyreportid>A-2</safetyreportid>
    <patient><reaction><reactionmeddrapt>Nausea</reactionmeddrapt></reaction></patient>
  </safetyreport>
</ichicsr>
"""


# --------------------------------------------------------------- a whole case

@pytest.fixture
def case():
    cases = parse_icsr(R2.encode(), product_names=["Vigilazine", "vigilazine"])
    assert len(cases) == 1
    return cases[0]


def test_the_identifiers_and_the_version(case):
    assert case.worldwide_case_id == "GB-ACME-2026001"
    assert case.local_case_ids == ["ACME-LOCAL-77"]
    assert case.case_version == 2


def test_the_two_receipt_dates_are_kept_apart(case):
    """The initial receipt places a case in an interval; the latest receipt is
    what the data lock point is compared against. Collapsing them into one
    would put every followed-up case in the wrong reporting period."""
    assert case.initial_receipt_date == date(2026, 2, 4)
    assert case.latest_receipt_date == date(2026, 3, 18)


def test_seriousness_is_read_and_its_criteria_with_it(case):
    assert case.is_serious is True
    assert case.seriousness_criteria == ["hospitalisation"]
    # `seriousnessdeath` was 2, which is "no". It is not in the list.
    assert "death" not in case.seriousness_criteria


def test_the_patient_fields(case):
    assert case.patient_age == 34.0
    assert case.patient_sex == "female"
    assert case.primary_reporter_qualification == "physician"
    assert case.is_medically_confirmed is True
    assert case.country_of_occurrence == "GB"
    assert case.report_source == "spontaneous"


def test_every_reaction_becomes_an_event_with_its_verbatim_term(case):
    assert len(case.events) == 2
    first = case.events[0]
    assert first.verbatim_term == "severe headache after second dose"
    assert first.meddra_pt == "Headache"
    assert first.meddra_llt == "Headache severe"
    assert first.meddra_version == "27.0"
    assert first.onset_date == date(2026, 2, 1)
    assert first.outcome == "recovered"


def test_the_parser_assigns_no_expectedness_or_causality(case):
    """Neither is an E2B field this module reads, and neither could be: both
    are determinations a qualified person makes against a pinned reference
    safety information version."""
    for event in case.events:
        assert not hasattr(event, "expectedness")
        assert not hasattr(event, "causality_company")


def test_drugs_carry_their_role_and_whether_they_are_ours(case):
    suspect = next(d for d in case.drugs if d.role == "suspect")
    assert suspect.drug_name == "VIGILAZINE 10 MG TABLETS"
    assert suspect.is_company_product is True
    assert suspect.dose == "10 mg once daily"
    assert suspect.indication == "Anxiety"
    assert suspect.start_date == date(2026, 1, 15)
    assert suspect.action_taken == "drug_withdrawn"

    concomitant = next(d for d in case.drugs if d.role == "concomitant")
    assert concomitant.drug_name == "Paracetamol"
    assert concomitant.is_company_product is False, (
        "an ICSR names a drug; it does not know whose portfolio it is in")


def test_the_narrative_comes_back_unmasked_and_is_the_callers_problem(case):
    """The parser does not mask. It hands the text over, and the pipeline puts
    it in the store that nothing reads."""
    assert "Jane Smith" in case.narrative
    assert "St Mary's Hospital" in case.narrative


# --------------------------------------------------- what is absent stays absent

def test_an_unstated_seriousness_is_not_a_determination_of_not_serious():
    case = parse_icsr(MINIMAL.encode())[0]
    assert case.is_serious is None
    assert "is_serious" in case.unmapped
    assert "determination" in case.unmapped["is_serious"]


def test_absent_fields_are_reported_rather_than_defaulted():
    case = parse_icsr(MINIMAL.encode())[0]
    assert "latest_receipt_date" in case.unmapped
    assert "country_of_occurrence" in case.unmapped
    assert case.latest_receipt_date is None


def test_a_case_with_a_criterion_and_no_flag_is_serious():
    """The criterion IS the statement. The reverse is not true and is not
    inferred: a case with a seriousness flag and no criteria stays as it is."""
    xml = MINIMAL.replace("<safetyreportid>MIN-1</safetyreportid>",
                          "<safetyreportid>MIN-1</safetyreportid>"
                          "<seriousnesslifethreatening>1</seriousnesslifethreatening>")
    case = parse_icsr(xml.encode())[0]
    assert case.is_serious is True
    assert case.seriousness_criteria == ["life_threatening"]


def test_an_uncoded_reaction_is_still_an_event():
    """A verbatim term with no MedDRA code is a real reported event. Dropping
    it would make it invisible to the tabulations AND to the grid that would
    have coded it."""
    case = parse_icsr(MINIMAL.encode())[0]
    assert len(case.events) == 1
    assert case.events[0].verbatim_term == "rash"
    assert case.events[0].meddra_pt is None


# --------------------------------------------------------------- partial dates

@pytest.mark.parametrize("raw,expected,precision", [
    ("20260304", date(2026, 3, 4), "day"),
    ("202603", date(2026, 3, 1), "month"),
    ("2026", date(2026, 1, 1), "year"),
    ("2026-03-04", date(2026, 3, 4), "day"),
    ("", None, None),
    (None, None, None),
    ("20261332", None, None),
])
def test_partial_dates_keep_the_precision_they_came_with(raw, expected, precision):
    """A reporter who knew the month and not the day gave a real answer. Storing
    the earliest day it could mean lets the value be compared; recording the
    precision stops the screen claiming a day nobody reported."""
    assert parse_date(raw) == (expected, precision)


def test_a_partial_receipt_date_is_recorded_as_partial():
    xml = R2.replace("<receivedate>20260204</receivedate>",
                     "<receivedate>202602</receivedate>")
    case = parse_icsr(xml.encode())[0]
    assert case.initial_receipt_date == date(2026, 2, 1)
    assert case.date_precision["initial_receipt_date"] == "month"


# ------------------------------------------------------------- several reports

def test_every_safety_report_in_the_file_becomes_a_case():
    cases = parse_icsr(TWO_REPORTS.encode())
    assert [c.worldwide_case_id for c in cases] == ["A-1", "A-2"]


def test_a_nested_report_is_not_read_twice():
    """`_all` walks descendants, so a report containing another matching
    element would be read once as itself and once as its child -- doubling a
    case, which doubles it in every figure downstream."""
    nested = """<?xml version="1.0"?>
    <ichicsr><safetyreport><safetyreportid>OUTER</safetyreportid>
      <patient><reaction><reactionmeddrapt>Headache</reactionmeddrapt></reaction></patient>
    </safetyreport></ichicsr>"""
    cases = parse_icsr(nested.encode())
    assert len(cases) == 1


# ------------------------------------------------------------ untrusted input

def test_an_empty_file_is_refused():
    with pytest.raises(E2bUnreadable):
        parse_icsr(b"")


def test_malformed_xml_is_refused_with_a_reason():
    with pytest.raises(E2bUnreadable) as raised:
        parse_icsr(b"<ichicsr><safetyreport>")
    assert "well-formed" in str(raised.value)


def test_a_file_with_no_case_in_it_is_refused():
    """An E2B acknowledgement is valid XML with none of a case in it. Read as
    an ICSR it would produce an empty case that counts as one."""
    ack = b"""<?xml version="1.0"?><ichicsrack><ichicsrmessageheader>
    <messagenumb>1</messagenumb></ichicsrmessageheader></ichicsrack>"""
    with pytest.raises(E2bUnreadable) as raised:
        parse_icsr(ack)
    assert "acknowledgement" in str(raised.value)


def test_declared_entities_are_refused_rather_than_expanded():
    """The billion-laughs shape. An ICSR has no legitimate reason to declare
    entities, and these files arrive from outside."""
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE ichicsr [
      <!ENTITY a "aaaaaaaaaa">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
    ]>
    <ichicsr><safetyreport><safetyreportid>&b;</safetyreportid></safetyreport></ichicsr>"""
    with pytest.raises(E2bUnreadable):
        parse_icsr(bomb)


def test_an_external_entity_is_not_fetched():
    """XXE: the classic way a parser is made to read a file off the server."""
    xxe = b"""<?xml version="1.0"?>
    <!DOCTYPE ichicsr [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <ichicsr><safetyreport><safetyreportid>&xxe;</safetyreportid>
    </safetyreport></ichicsr>"""
    with pytest.raises(E2bUnreadable):
        parse_icsr(xxe)


# ---------------------------------------------------------- the R3 shape too

def test_a_namespaced_r3_document_reads_by_local_name():
    """R3 is HL7 v3 and namespaced; R2 is bare. The namespace is exactly the
    difference this parser is arranged not to care about."""
    r3 = """<?xml version="1.0"?>
    <MCCI_IN200100UV01 xmlns="urn:hl7-org:v3">
      <safetyreport>
        <safetyreportid>R3-0001</safetyreportid>
        <receiptdate>20260301</receiptdate>
        <patient>
          <reaction><reactionmeddrapt>Dizziness</reactionmeddrapt></reaction>
        </patient>
      </safetyreport>
    </MCCI_IN200100UV01>"""
    case = parse_icsr(r3.encode())[0]
    assert case.worldwide_case_id == "R3-0001"
    assert case.latest_receipt_date == date(2026, 3, 1)
    assert case.events[0].meddra_pt == "Dizziness"
