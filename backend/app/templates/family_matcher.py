"""Bulk onboarding: group an estate into families, and mint one per group.

§11 is the reason this exists -- "Thousands of files should not imply thousands
of independent manual mappings" -- and §18 prices it: ~15-40 model calls to
onboard a template with no family match against ~2-6 with a strong one, which
is why the doc concludes "family reuse is the primary cost lever, not model
selection". Every file that fails to join a family is that difference, billed.

This module used to cluster by TF-IDF cosine over the full document text with
English stopwords, which is the one signal guaranteed to fail on the estates the
product is aimed at. A German offer letter and its English twin share almost no
vocabulary, so they scored near zero and landed in separate clusters; the estate
then produced one manifest per translation instead of one per family, and the
collapse the whole architecture depends on never happened. Text similarity also
groups unrelated documents that share boilerplate -- two different letters with
the same three legal paragraphs at the bottom look alike to a bag of words.

So the clustering here is the structural fingerprint from
`app.templates.fingerprint`: MERGEFIELD codes first, then table shape, colour
pattern and paragraph band. Those signals are language-independent by
construction, which is what makes a family a document type rather than a
language. The scoring, the union-find and the choice of representative live
there; this module is the bulk-onboarding shape around them -- ids the API
speaks, and the fingerprint the resulting family is stored with.
"""

from dataclasses import dataclass

from app.templates.fingerprint import (
    StructuralFingerprint, cluster_by_structure, fingerprint_file,
)

#: The structural score at which two templates are the same family. Kept equal
#: to `cluster_by_structure`'s own default rather than tuned separately: two
#: numbers that mean "same family" and can drift apart is how an estate ends up
#: clustered one way at upload and matched another way at inheritance.
#: `app.templates.inheritance.TARGETED_REVIEW_SIMILARITY` is the same floor seen
#: from the other side, and carries the reasoning for its value.
SIMILARITY_THRESHOLD = 0.6


@dataclass
class ClusterMemberResult:
    template_file_id: str
    name: str
    similarity_to_representative: float
    is_representative: bool


@dataclass
class ClusterResult:
    label: str
    members: list  # list[ClusterMemberResult]
    representative_template_file_id: str
    # The representative's fingerprint, which is what a `template_families` row
    # stores. Returned rather than recomputed by the caller: the file has just
    # been prescanned here, and a family whose fingerprint was computed from a
    # second read of a file that has since been archived is a family that can
    # match nothing.
    fingerprint: StructuralFingerprint = None
    representative_template_version_id: str | None = None


def cluster_templates(items: list[dict]) -> list[ClusterResult]:
    """`items`: [{"template_file_id", "name", "path", "template_version_id"?}, ...]
    -- already-uploaded template blobs on local disk. Returns one ClusterResult
    per discovered family, each with a chosen representative.

    A file that cannot be prescanned raises rather than being dropped into a
    cluster of its own: a template nobody could read has no structure to compare,
    and silently making it a family of one would hide the parse failure behind a
    plausible-looking onboarding report.
    """
    if not items:
        return []

    families = cluster_by_structure(
        [{"name": it["name"], "path": it["path"]} for it in items],
        threshold=SIMILARITY_THRESHOLD,
    )

    clusters: list[ClusterResult] = []
    for family in families:
        representative = items[family.representative_index]
        clusters.append(ClusterResult(
            label=family.label,
            representative_template_file_id=representative["template_file_id"],
            representative_template_version_id=representative.get("template_version_id"),
            fingerprint=fingerprint_file(representative["path"]),
            members=[
                ClusterMemberResult(
                    template_file_id=items[i]["template_file_id"],
                    name=items[i]["name"],
                    similarity_to_representative=round(float(family.scores.get(i, 0.0)), 3),
                    is_representative=(i == family.representative_index),
                )
                for i in sorted(family.member_indices)
            ],
        ))
    return clusters
