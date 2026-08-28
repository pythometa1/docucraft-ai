"""Group structurally-similar templates, across languages.

Clustering by text similarity — what `template_clustering.py` does with TF-IDF
and English stopwords — clusters an estate by *language*, not by document type.
A German offer letter and its English twin share almost no vocabulary, so they
never group, and the estate produces one manifest per translation instead of
one per family. That defeats the entire point: the collapse from thousands of
templates to a few hundred families is what makes onboarding tractable.

The signals here are language-independent by construction:

  MERGEFIELD codes  strongest — field codes like `LAB__FT_SALARY__38_HR_` are
                    typically identical across localized variants of a template
  table shape       column counts survive translation
  colour pattern    where the blue/red runs fall
  structure         paragraph-count band, heading depth, hyperlink count

Text similarity stays available as a secondary signal *within* a language.
"""

from dataclasses import dataclass, field

from app.templates.parsers.docx_prescan import PreScanResult, prescan


@dataclass
class StructuralFingerprint:
    mergefield_codes: frozenset = frozenset()
    paragraph_band: int = 0
    table_count: int = 0
    table_shapes: tuple = ()
    blue_span_count: int = 0
    red_span_count: int = 0
    hyperlink_count: int = 0
    bracket_tokens: frozenset = frozenset()

    def as_dict(self) -> dict:
        return {
            "mergefield_codes": sorted(self.mergefield_codes),
            "paragraph_band": self.paragraph_band,
            "table_count": self.table_count,
            "table_shapes": list(self.table_shapes),
            "blue_span_count": self.blue_span_count,
            "red_span_count": self.red_span_count,
            "hyperlink_count": self.hyperlink_count,
            "bracket_tokens": sorted(self.bracket_tokens),
        }


def _band(count: int) -> int:
    """Bucket paragraph counts so a translation that runs a little longer or
    shorter than its source still lands in the same band."""
    if count <= 20:
        return 0
    if count <= 60:
        return 1
    if count <= 150:
        return 2
    if count <= 300:
        return 3
    return 4


def fingerprint_from_prescan(scan: PreScanResult) -> StructuralFingerprint:
    from app.templates.parsers.docx_prescan import extract_brackets

    brackets = set()
    for span in scan.spans:
        for token in extract_brackets(span.text):
            brackets.add(token.strip().lower())

    table_paragraphs = len(scan.table_paragraph_indices)
    return StructuralFingerprint(
        mergefield_codes=frozenset(mf.code for mf in scan.mergefields),
        paragraph_band=_band(len(scan.paragraphs)),
        table_count=1 if table_paragraphs else 0,
        table_shapes=(table_paragraphs,),
        blue_span_count=sum(1 for s in scan.spans if s.color == "blue" and s.text.strip()),
        red_span_count=sum(1 for s in scan.spans if s.color == "red" and s.text.strip()),
        hyperlink_count=len(scan.hyperlinks),
        bracket_tokens=frozenset(brackets),
    )


def fingerprint_file(path: str) -> StructuralFingerprint:
    return fingerprint_from_prescan(prescan(path))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _closeness(a: int, b: int) -> float:
    if a == b == 0:
        return 1.0
    return 1 - abs(a - b) / max(a, b, 1)


def structural_similarity(a: StructuralFingerprint, b: StructuralFingerprint) -> float:
    """0..1. Weighted toward MERGEFIELD codes, which are the signal least
    disturbed by translation."""
    mergefields = _jaccard(a.mergefield_codes, b.mergefield_codes)
    brackets = _jaccard(a.bracket_tokens, b.bracket_tokens)
    shape = (
        (1.0 if a.paragraph_band == b.paragraph_band else 0.0) * 0.4
        + _closeness(a.table_shapes[0] if a.table_shapes else 0, b.table_shapes[0] if b.table_shapes else 0) * 0.2
        + _closeness(a.blue_span_count, b.blue_span_count) * 0.2
        + _closeness(a.red_span_count, b.red_span_count) * 0.2
    )

    # An absent signal is absent, not zero -- redistribute its weight rather
    # than penalising both templates for a convention neither of them uses.
    # This applied to mergefields and not to bracket tokens, which capped a
    # pure-MERGEFIELD estate at 0.8 even for a file compared against itself:
    # two identical templates could never be called a revision of each other,
    # which is precisely the case §11's "similarity very high" branch exists to
    # catch.
    has_mergefields = bool(a.mergefield_codes or b.mergefield_codes)
    has_brackets = bool(a.bracket_tokens or b.bracket_tokens)
    if not (has_mergefields or has_brackets):
        # Shape alone is not identity. Neither template names a single thing to
        # fill, so a matching paragraph band and table count is a coincidence
        # worth reporting weakly and never worth inheriting a manifest on.
        return round(0.55 * shape, 4)

    present = [(shape, 0.3)]
    if has_mergefields:
        present.append((mergefields, 0.5))
    if has_brackets:
        present.append((brackets, 0.2))
    total = sum(weight for _score, weight in present)
    return round(sum(score * weight for score, weight in present) / total, 4)


@dataclass
class Family:
    label: str
    representative_index: int
    member_indices: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)


def cluster_by_structure(items: list[dict], threshold: float = 0.6) -> list[Family]:
    """`items`: [{"name": str, "path": str}, ...] -> one Family per group.

    Union-find over the structural similarity matrix; the representative is the
    member with the highest average similarity to the rest, so the manifest is
    compiled from the most typical file rather than an outlier.
    """
    if not items:
        return []
    fingerprints = [fingerprint_file(it["path"]) for it in items]
    n = len(items)
    if n == 1:
        return [Family(items[0]["name"], 0, [0], {0: 1.0})]

    similarity = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            score = structural_similarity(fingerprints[i], fingerprints[j])
            similarity[i][j] = similarity[j][i] = score

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if similarity[i][j] >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    families = []
    for members in groups.values():
        if len(members) == 1:
            representative = members[0]
        else:
            averages = {
                i: sum(similarity[i][j] for j in members if j != i) / (len(members) - 1)
                for i in members
            }
            representative = max(averages, key=averages.get)
        families.append(Family(
            label=items[representative]["name"],
            representative_index=representative,
            member_indices=members,
            scores={i: (1.0 if i == representative else similarity[i][representative]) for i in members},
        ))
    return families
