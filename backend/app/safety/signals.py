"""Signal management, and the screening statistic that feeds it.

Two rules from the spec shape everything here.

**A threshold crossing is not a signal.** Disproportionality says a term is
reported more often with this product than with the background; it says
nothing about why. So a screen never creates a signal. It returns figures,
and a person may raise a *candidate* from one -- which is not a signal either
until somebody validates it. GVP Module IX's own order: detection, validation,
then analysis.

**The statistic is labelled wherever it appears.** Every result carries
`DISCLAIMER`, verbatim, and the export and the screen print it beside the
numbers rather than in a footnote.

The arithmetic is plain and shown: the 2x2 table behind every figure is
returned with it, so a reviewer can check PRR = (a/(a+b)) / (c/(c+d)) with a
calculator. A cell that makes a statistic undefined gives no statistic and
says which cell, rather than a continuity-corrected number that looks like
evidence.
"""

import math
from dataclasses import dataclass, field

DISCLAIMER = ("Screening statistic — indicates reporting frequency, not causality; "
              "not evidence of a causal association.")

#: candidate -> new (validated) -> ongoing -> closed, or candidate -> refuted.
#: A refuted candidate stays on the log with its reason: a screen that raised
#: the same term every quarter should show it was looked at every quarter.
STATUSES = ("candidate", "new", "ongoing", "closed", "refuted")

TRANSITIONS = {
    "candidate": {"new", "refuted"},
    "new": {"ongoing", "closed"},
    "ongoing": {"closed"},
    "closed": {"ongoing"},       # reopened on new information
    "refuted": {"candidate"},    # raised again on new information
}

#: Moves a reviewer makes rather than a writer: deciding a candidate is a
#: signal (or is not), and closing one.
REVIEWER_TRANSITIONS = {("candidate", "new"), ("candidate", "refuted"),
                        ("new", "closed"), ("ongoing", "closed")}

DETECTION_SOURCES = ("disproportionality", "case_review", "literature",
                     "authority_request", "trial", "other")

PRIORITIES = ("high", "medium", "low")

#: Evans et al. (2001): a >= 3, PRR >= 2, chi-squared >= 4.
EVANS_MIN_CASES = 3
EVANS_MIN_PRR = 2.0
EVANS_MIN_CHI2 = 4.0

Z_95 = 1.959963984540054


class SignalError(Exception):
    """A signal change that is not allowed."""


def check_transition(before: str, after: str) -> None:
    if after not in STATUSES:
        raise SignalError(f"status must be one of {', '.join(STATUSES)}")
    if before == after:
        return
    if after not in TRANSITIONS.get(before, set()):
        allowed = ", ".join(sorted(TRANSITIONS.get(before, set()))) or "nothing"
        raise SignalError(f"a {before} signal can become {allowed}, not {after}")


def needs_reviewer(before: str, after: str) -> bool:
    return (before, after) in REVIEWER_TRANSITIONS


def closing_problems(*, conclusion, action_taken) -> list:
    """What a signal needs before it can be closed: GVP IX asks what was
    concluded and what was done about it, and a closed signal without either
    is a question nobody answered."""
    problems = []
    if not (conclusion or "").strip():
        problems.append("a conclusion")
    if not (action_taken or "").strip():
        problems.append("the action taken (or 'none')")
    return problems


# ---------------------------------------------------------- disproportionality

@dataclass
class Screen:
    """One term's 2x2 table and what it gives.

    a: cases with this product reporting the term
    b: cases with this product not reporting it
    c: background cases reporting the term
    d: background cases not reporting it
    """

    term: str
    a: int
    b: int
    c: int
    d: int
    prr: float | None = None
    prr_ci: tuple | None = None
    ror: float | None = None
    ror_ci: tuple | None = None
    chi2: float | None = None
    notes: list = field(default_factory=list)

    @property
    def meets_evans(self) -> bool:
        return (self.a >= EVANS_MIN_CASES and self.prr is not None
                and self.prr >= EVANS_MIN_PRR and self.chi2 is not None
                and self.chi2 >= EVANS_MIN_CHI2)

    @property
    def ror_lower_above_one(self) -> bool:
        return (self.a >= EVANS_MIN_CASES and self.ror_ci is not None
                and self.ror_ci[0] > 1)

    def as_dict(self) -> dict:
        return {
            "term": self.term, "a": self.a, "b": self.b, "c": self.c, "d": self.d,
            "prr": _round(self.prr), "prr_ci": _round_pair(self.prr_ci),
            "ror": _round(self.ror), "ror_ci": _round_pair(self.ror_ci),
            "chi2": _round(self.chi2), "notes": list(self.notes),
            "meets_evans": self.meets_evans,
            "ror_lower_above_one": self.ror_lower_above_one,
            "screening_flag": self.meets_evans or self.ror_lower_above_one,
        }


def _round(value, places=2):
    return None if value is None else round(value, places)


def _round_pair(pair):
    return None if pair is None else [round(pair[0], 2), round(pair[1], 2)]


def screen(term: str, a: int, b: int, c: int, d: int) -> Screen:
    """PRR, ROR and Yates-corrected chi-squared for one 2x2 table, each with
    its 95% interval where it is defined."""
    for name, value in (("a", a), ("b", b), ("c", c), ("d", d)):
        if value < 0:
            raise ValueError(f"cell {name} is negative ({value})")
    out = Screen(term=term, a=a, b=b, c=c, d=d)
    if a == 0:
        out.notes.append("the term is not reported with this product (a = 0)")
        return out
    if c == 0:
        out.notes.append("the term is not reported in the background (c = 0); no "
                         "ratio can be formed")
    else:
        out.prr = (a / (a + b)) / (c / (c + d))
        se = math.sqrt(1 / a - 1 / (a + b) + 1 / c - 1 / (c + d))
        out.prr_ci = (math.exp(math.log(out.prr) - Z_95 * se),
                      math.exp(math.log(out.prr) + Z_95 * se))
    if b == 0 or c == 0 or d == 0:
        if b == 0:
            out.notes.append("every case with this product reports the term (b = 0); "
                             "the odds ratio is undefined")
        elif d == 0:
            out.notes.append("every background case reports the term (d = 0); the "
                             "odds ratio is undefined")
    else:
        out.ror = (a * d) / (b * c)
        se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
        out.ror_ci = (math.exp(math.log(out.ror) - Z_95 * se),
                      math.exp(math.log(out.ror) + Z_95 * se))
    n = a + b + c + d
    denominator = (a + b) * (c + d) * (a + c) * (b + d)
    if denominator:
        out.chi2 = n * max(0.0, abs(a * d - b * c) - n / 2) ** 2 / denominator
    return out


def screen_all(drug_terms: dict, drug_total: int, background_terms: dict,
               background_total: int, *, min_cases: int = 1) -> list:
    """Every term reported with this product, screened against the background.

    `drug_terms` / `background_terms` map a term to the number of CASES
    reporting it (a case reporting a term twice is one case); the totals are
    the number of cases with any coded term.
    """
    rows = []
    for term, a in drug_terms.items():
        if a < min_cases:
            continue
        c = background_terms.get(term, 0)
        rows.append(screen(term, a, drug_total - a, c, background_total - c))
    rows.sort(key=lambda s: (not (s.meets_evans or s.ror_lower_above_one),
                             -(s.prr or 0), -s.a, s.term))
    return rows
