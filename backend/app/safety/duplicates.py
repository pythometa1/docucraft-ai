"""Finding cases that might be the same case, and never deciding that they are.

A duplicate counted twice inflates every figure in the report; two distinct
cases merged loses one entirely. Both are wrong, both are permanent, and
neither is visible in the output -- so §6's fourth stage says candidates go to
a review queue and are never merged automatically, and this module implements
exactly that: it scores pairs and stops.

**The score is a sum of agreements, not a similarity.** Every contribution
names the field it came from, so the review screen can say "same country, same
onset date, same preferred term, ages within a year" rather than "0.82". A
reviewer deciding whether two ICSRs are one case needs the evidence, not a
number they cannot audit.

**Nothing matches on one field alone.** A shared country is not evidence; a
shared preferred term is not evidence. The threshold requires agreement across
independent dimensions, because a product with four thousand headache reports
would otherwise produce four thousand candidate pairs and a queue nobody reads.
"""

from dataclasses import dataclass, field

from sqlalchemy import select

#: What each agreement is worth. Weighted by how much it narrows the field: a
#: shared worldwide case identifier is nearly decisive, a shared country is
#: barely a hint.
WEIGHTS = {
    "worldwide_case_id": 0.9,
    "local_case_id": 0.7,
    "onset_date": 0.35,
    "receipt_date": 0.2,
    "preferred_terms": 0.3,
    "country": 0.1,
    "patient_sex": 0.1,
    "patient_age": 0.2,
    "reporter_qualification": 0.05,
    "suspect_drug": 0.1,
}

#: The score at which a pair is worth a person's time.
THRESHOLD = 0.6

#: How many independent fields must agree, whatever the score says. Two cases
#: agreeing only on country and sex score enough to pass a naive threshold and
#: are not evidence of anything.
MIN_FIELDS = 2

#: Ages this close are treated as the same age. An ICSR reporting 34 and a
#: line listing reporting 34.5 are the same person to within the precision
#: either source has.
AGE_TOLERANCE_YEARS = 1.0


@dataclass
class Candidate:
    case_id: str
    other_case_id: str
    score: float
    matched_on: list = field(default_factory=list)

    def as_row(self) -> dict:
        return {"case_id": self.case_id, "other_case_id": self.other_case_id,
                "score": round(self.score, 3), "matched_on": list(self.matched_on)}


def _terms(events) -> set:
    return {(e.meddra_pt or e.verbatim_term or "").strip().lower()
            for e in events if (e.meddra_pt or e.verbatim_term)}


def compare(left, right, *, left_events=(), right_events=(),
            left_drugs=(), right_drugs=()) -> Candidate | None:
    """Score one pair, or None if they are not worth asking about.

    Deliberately symmetric and side-effect free, so the same pair scores the
    same however it is reached and the function can be tested on two objects
    rather than through a database.
    """
    matched: list = []
    score = 0.0

    def agree(field_name: str, note: str) -> None:
        nonlocal score
        score += WEIGHTS[field_name]
        matched.append(note)

    if left.worldwide_case_id and left.worldwide_case_id == right.worldwide_case_id:
        agree("worldwide_case_id",
              f"the same worldwide case identifier ({left.worldwide_case_id})")
    shared_local = set(left.local_case_ids or []) & set(right.local_case_ids or [])
    if shared_local:
        agree("local_case_id",
              f"a shared local identifier ({sorted(shared_local)[0]})")

    if left.country_of_occurrence and \
            left.country_of_occurrence == right.country_of_occurrence:
        agree("country", f"the same country ({left.country_of_occurrence})")
    if left.patient_sex and left.patient_sex == right.patient_sex:
        agree("patient_sex", f"the same patient sex ({left.patient_sex})")
    if left.patient_age is not None and right.patient_age is not None \
            and abs(left.patient_age - right.patient_age) <= AGE_TOLERANCE_YEARS:
        agree("patient_age",
              f"ages within a year ({left.patient_age} and {right.patient_age})")
    if left.primary_reporter_qualification and \
            left.primary_reporter_qualification == right.primary_reporter_qualification:
        agree("reporter_qualification",
              f"the same reporter qualification ({left.primary_reporter_qualification})")
    if left.initial_receipt_date and \
            left.initial_receipt_date == right.initial_receipt_date:
        agree("receipt_date", f"the same receipt date ({left.initial_receipt_date})")

    shared_terms = _terms(left_events) & _terms(right_events)
    if shared_terms:
        agree("preferred_terms",
              f"{len(shared_terms)} shared reported term(s): "
              + ", ".join(sorted(shared_terms)[:3]))

    onsets_left = {e.onset_date for e in left_events if e.onset_date}
    onsets_right = {e.onset_date for e in right_events if e.onset_date}
    shared_onsets = onsets_left & onsets_right
    if shared_onsets:
        agree("onset_date", f"the same event onset date ({sorted(shared_onsets)[0]})")

    drugs_left = {(d.drug_name or "").strip().lower() for d in left_drugs
                  if d.is_company_product and d.drug_name}
    drugs_right = {(d.drug_name or "").strip().lower() for d in right_drugs
                   if d.is_company_product and d.drug_name}
    if drugs_left & drugs_right:
        agree("suspect_drug", "the same suspect product")

    if len(matched) < MIN_FIELDS or score < THRESHOLD:
        return None
    return Candidate(case_id=left.id, other_case_id=right.id, score=score,
                     matched_on=matched)


def find(db, *, pv_product_id: str, org_id: str, limit: int = 500) -> list:
    """Candidate pairs across one product's case store.

    Blocked on country and sex before scoring: comparing every case with every
    other is quadratic, and §14 says a hundred thousand cases per product. Two
    cases that share neither country nor sex are not a duplicate pair worth the
    comparison, and the block is what keeps this from being a query nobody can
    run twice.
    """
    from app.models import PvCase, PvCaseDrug, PvCaseEvent

    cases = db.scalars(select(PvCase).where(
        PvCase.pv_product_id == pv_product_id).limit(limit)).all()
    if len(cases) < 2:
        return []

    events: dict = {}
    for event in db.scalars(select(PvCaseEvent).where(
            PvCaseEvent.pv_product_id == pv_product_id)).all():
        events.setdefault(event.case_id, []).append(event)
    drugs: dict = {}
    for drug in db.scalars(select(PvCaseDrug).where(
            PvCaseDrug.pv_product_id == pv_product_id)).all():
        drugs.setdefault(drug.case_id, []).append(drug)

    blocks: dict = {}
    for case in cases:
        key = ((case.country_of_occurrence or "").lower(),
               (case.patient_sex or "").lower())
        blocks.setdefault(key, []).append(case)
        # A case with a worldwide identifier is also compared against every
        # other case carrying the same one, whatever its country: an identifier
        # that matches outranks a block that does not.
        if case.worldwide_case_id:
            blocks.setdefault(("id", case.worldwide_case_id), []).append(case)

    found: dict = {}
    for group in blocks.values():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                pair = tuple(sorted((left.id, right.id)))
                if pair in found:
                    continue
                candidate = compare(
                    left, right,
                    left_events=events.get(left.id, []),
                    right_events=events.get(right.id, []),
                    left_drugs=drugs.get(left.id, []),
                    right_drugs=drugs.get(right.id, []))
                if candidate is not None:
                    found[pair] = candidate
    return sorted(found.values(), key=lambda c: -c.score)


def record(db, *, pv_product_id: str, org_id: str, candidates) -> int:
    """Store candidates that are not already recorded.

    A pair a person has already answered is not raised again. Re-asking about a
    pair somebody has decided is "keep both" is how a queue becomes a thing
    people dismiss rather than read.
    """
    from app.models import PvDuplicateCandidate

    existing = set()
    for row in db.scalars(select(PvDuplicateCandidate).where(
            PvDuplicateCandidate.pv_product_id == pv_product_id)).all():
        existing.add(tuple(sorted((row.case_id, row.other_case_id))))

    written = 0
    for candidate in candidates:
        pair = tuple(sorted((candidate.case_id, candidate.other_case_id)))
        if pair in existing:
            continue
        db.add(PvDuplicateCandidate(
            org_id=org_id, pv_product_id=pv_product_id,
            case_id=candidate.case_id, other_case_id=candidate.other_case_id,
            score=round(candidate.score, 3), matched_on=candidate.matched_on,
            status="pending"))
        existing.add(pair)
        written += 1
    return written
