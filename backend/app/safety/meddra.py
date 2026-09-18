"""Coding a reported term, or admitting that it has not been coded.

MedDRA is licensed. This repository does not ship it and cannot, so the
important behaviour of this module is what it does when no dictionary is
loaded: **nothing**. `meddra_pt` stays null, the event is flagged
`coding_required`, and it appears in the review grid for a person to code.

That is the whole design decision, and it is worth being blunt about why. A
guessed code is not a smaller version of a real one. Every tabulation in a
periodic safety report is grouped by System Organ Class and Preferred Term; a
wrong PT puts an event under the wrong SOC, in a total a regulator compares
against the last report, and nothing about the output looks wrong. An uncoded
event is visible -- it sits in a queue with a number beside it -- and a
miscoded one is not.

So: no fuzzy matching, no nearest-neighbour, no model. A term is coded when it
matches a dictionary entry, and otherwise it is not coded.

**The version travels with the code.** MedDRA changes twice a year: terms are
added, and a term's PT can move between SOCs. An event coded under 26.1 and
tabulated under 27.0 is filed under a hierarchy it was never assigned, so every
coded event records the version it was coded against, and §11's sixth check
compares them across a report.

Loading: a dictionary is a table of `llt, pt, hlt, hlgt, soc` rows supplied by
the licensee. `load_csv` reads one; `Dictionary.EMPTY` is what a deployment
without a licence gets, and it codes nothing.
"""

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    """One row of the hierarchy, as the dictionary states it."""

    llt: str
    pt: str
    hlt: str | None = None
    hlgt: str | None = None
    soc: str | None = None


class Dictionary:
    """A loaded MedDRA version, or the empty one.

    Lookup is exact on a normalised form: case, surrounding whitespace and
    internal runs of spaces are not meaningful, and everything else is. A
    dictionary that matched "headaches" to "Headache" would be doing the fuzzy
    matching this module exists to refuse.
    """

    def __init__(self, version: str | None = None, terms=()):
        self.version = version
        self._by_llt: dict = {}
        self._by_pt: dict = {}
        for term in terms:
            self._by_llt.setdefault(_fold(term.llt), term)
            self._by_pt.setdefault(_fold(term.pt), term)

    @property
    def loaded(self) -> bool:
        """False when no dictionary is licensed into this deployment."""
        return bool(self._by_llt or self._by_pt)

    def __len__(self) -> int:
        return len(self._by_llt)

    def lookup(self, verbatim: str | None) -> Term | None:
        """The hierarchy for a reported term, or None.

        None is a real answer and the caller must treat it as one: it means
        this event is not coded, not that it codes to something approximate.
        """
        key = _fold(verbatim)
        if not key:
            return None
        return self._by_llt.get(key) or self._by_pt.get(key)


#: What a deployment without a MedDRA licence has. Codes nothing, on purpose.
Dictionary.EMPTY = Dictionary()


def _fold(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def load_csv(data, *, version: str) -> Dictionary:
    """A dictionary from a licensee's export.

    Columns are matched by name so an export with extra columns still loads:
    `llt` and `pt` are required, the rest of the hierarchy is optional and
    absent levels stay absent rather than being inferred from the PT.
    """
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("the dictionary file has no header row")
    columns = {_fold(name): name for name in reader.fieldnames}
    if "llt" not in columns or "pt" not in columns:
        raise ValueError(
            "a MedDRA export needs at least an 'llt' and a 'pt' column; found "
            + ", ".join(reader.fieldnames))

    def cell(row, key):
        name = columns.get(key)
        return (row.get(name) or "").strip() or None if name else None

    terms = []
    for row in reader:
        llt, pt = cell(row, "llt"), cell(row, "pt")
        if not llt or not pt:
            continue
        terms.append(Term(llt=llt, pt=pt, hlt=cell(row, "hlt"),
                          hlgt=cell(row, "hlgt"), soc=cell(row, "soc")))
    return Dictionary(version=version, terms=terms)


@dataclass
class Coding:
    """What coding one event produced, and why."""

    llt: str | None = None
    pt: str | None = None
    hlt: str | None = None
    hlgt: str | None = None
    soc: str | None = None
    version: str | None = None
    coded: bool = False
    reason: str = ""


def code_term(verbatim: str | None, dictionary: Dictionary) -> Coding:
    """Code one reported term, or say why it was not coded.

    The `reason` is written to the review grid, because "not coded" and "not
    coded BECAUSE no dictionary is loaded" send a person to two different
    places -- one to the term, the other to their MedDRA licence.
    """
    if not (verbatim or "").strip():
        return Coding(reason="the event carries no reported term to code")
    if not dictionary.loaded:
        return Coding(reason=(
            "no MedDRA dictionary is loaded in this deployment, so nothing is "
            "coded automatically; code this event by hand"))
    term = dictionary.lookup(verbatim)
    if term is None:
        return Coding(reason=(
            f"{verbatim!r} matches no term in MedDRA {dictionary.version}; it may "
            "need a synonym or a manual code"))
    return Coding(llt=term.llt, pt=term.pt, hlt=term.hlt, hlgt=term.hlgt,
                  soc=term.soc, version=dictionary.version, coded=True,
                  reason=f"exact match in MedDRA {dictionary.version}")


def dictionary_for(db, org_id: str, version: str | None):
    """The dictionary this organisation has licensed, if any.

    A hook rather than an implementation: a deployment that holds a licence
    wires its own loading in here. The default is `Dictionary.EMPTY`, which
    codes nothing -- and a system that codes nothing is a system whose
    tabulations are honestly empty rather than confidently wrong.
    """
    return Dictionary.EMPTY
