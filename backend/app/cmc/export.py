"""Assembling a dossier: approved sections in CTD order, with their tables
rendered from verified data at the moment of export.

Two things this module refuses to do, both on purpose.

It never rebuilds a document from editor HTML. Section content is stored as
text and tables are built from the store, so what a reviewer approved is what
gets written -- an HTML round trip is how a table becomes a paragraph of
numbers separated by tabs.

It never resolves a `[TABLE: key]` marker at draft time. The marker is
resolved HERE, so a value corrected in the Data Review grid appears in every
deliverable that quotes it without a single section being regenerated. That is
the property the whole Flow A / Flow B split exists to buy.

What this module does NOT attempt: an eCTD backbone or its XML. Leaf files and
a manifest are produced with conventional names; assembling and validating a
submission is a publishing tool's job, and a module that half-did it would
produce something that looks submittable and is not.
"""

import os
from dataclasses import dataclass, field

from app.docgen import assembly as _assembly
from app.docgen.markers import TABLE_MARKER_RE as _SHARED_TABLE_MARKER_RE
from app.templates import blueprint as bp

#: `[TABLE: spec_table]` on a line of its own. Matched with its surrounding
#: blank lines so removing it does not leave a hole in the prose.
# The shared grammar -- see `app.docgen.markers`.
TABLE_MARKER_RE = _SHARED_TABLE_MARKER_RE

#: eCTD leaf file names, by the section a leaf begins at. Conventional rather
#: than authoritative: the publishing tool renames as its own template
#: dictates, and what matters here is that one leaf is one file with a name a
#: person can recognise.
LEAF_NAMES = {
    "S.1": "32s1-gen-info", "S.2": "32s2-manuf", "S.3": "32s3-charac",
    "S.4": "32s4-contr-drug-sub", "S.5": "32s5-ref-stand",
    "S.6": "32s6-cont-closure-sys", "S.7": "32s7-stab",
    "P.1": "32p1-desc-comp", "P.2": "32p2-pharm-dev", "P.3": "32p3-manuf",
    "P.4": "32p4-contr-excip", "P.5": "32p5-contr-drug-prod",
    "P.6": "32p6-ref-stand", "P.7": "32p7-cont-closure-sys", "P.8": "32p8-stab",
    "A.1": "32a1-fac-equip", "A.2": "32a2-advent-agents", "A.3": "32a3-novel-excip",
    "R.1": "32r-reg-info",
}


class ExportBlocked(Exception):
    """The dossier is not in a state that may leave the building."""


@dataclass
class SectionRender:
    section_code: str
    title: str
    level: int
    content: str
    table_keys: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)


@dataclass
class ExportPlan:
    """What an export would contain, decided before a byte is written.

    Separate from the writing so the UI can show it and the gate can refuse it
    without a document existing first: an export that fails halfway leaves a
    file somebody might send.
    """

    sections: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    leaves: dict = field(default_factory=dict)


def leaf_for(section_code: str) -> str:
    """The eCTD leaf a section belongs to: its top two numbering parts."""
    parts = (section_code or "").split(".")
    for depth in (2, 1):
        key = ".".join(parts[:depth])
        if key in LEAF_NAMES:
            return key
    return parts[0] if parts else "other"


def heading_level(section_code: str) -> int:
    """Heading depth from the numbering, so 3.2.P.5.1 is a level below 3.2.P.5
    without anybody maintaining a second list of levels."""
    return min(1 + max(0, len((section_code or "").split(".")) - 1), 4)


# The shared layout -- see `app.docgen.assembly`. Kept under these names so
# nothing that imported them from here changes.
split_on_tables = _assembly.split_on_tables
_paragraphs = _assembly.paragraphs


def section_blocks(section: SectionRender, tables: dict) -> list:
    """One section as blueprint blocks: its heading, then its parts in order.

    `tables` maps a table key to the blocks that table renders as -- resolved
    by the caller, because deciding what a table contains is a database
    question and this function is about layout.
    """
    style = f"Heading {section.level}" if section.level <= 4 else None
    blocks = [bp.paragraph(
        [bp.segment("static", f"{section.section_code} {section.title}")], style=style)]
    for kind, value in split_on_tables(section.content):
        if kind == "text":
            blocks.extend(_paragraphs(value))
        else:
            rendered = tables.get(value)
            if rendered is None:
                # A marker with no table behind it is a hole in the document,
                # and it is written as one rather than dropped: a silently
                # missing specification table reads as a section that never
                # needed one.
                blocks.append(bp.paragraph([bp.segment(
                    "static", f"[TABLE NOT AVAILABLE: {value}]")]))
            else:
                blocks.extend(rendered)
    return blocks


def build_document(sections: list, tables_by_section: dict, *, title: str,
                   subtitle: str | None = None) -> dict:
    """A whole deliverable as one normalised blueprint body."""
    blocks = [bp.paragraph([bp.segment("static", title)], style="Title")]
    if subtitle:
        blocks.append(bp.paragraph([bp.segment("static", subtitle)]))
    for section in sections:
        blocks.extend(section_blocks(section, tables_by_section.get(section.section_code, {})))
    return bp.normalise_body({"blocks": blocks, "sect_pr_from": None})


def write_docx(body: dict, output_path: str) -> str:
    """Write a body and give its tables borders.

    `emit` verifies its own output by reading it back, which is why the border
    pass runs after it returns rather than before: the verification is on the
    document this module actually produced, and the styling is cosmetic
    afterwards. `emit` writes no tblStyle of its own, so an unstyled table has
    no borders at all -- which on a specification table is unreadable.
    """
    from app.templates.emit_docx import emit

    emit(body, output_path)
    try:
        import docx as docx_lib

        document = docx_lib.Document(output_path)
        changed = False
        for table in document.tables:
            table.style = "Table Grid"
            changed = True
        if changed:
            document.save(output_path)
    except Exception:  # noqa: BLE001 - borders are cosmetic; the content is not
        pass
    return output_path


def manifest_lines(leaves: dict) -> str:
    """Section code to filename, as the publishing tool's operator needs it."""
    lines = ["# CTD leaf manifest", "# section_code\tfile"]
    for leaf, entry in sorted(leaves.items()):
        for code in entry["sections"]:
            lines.append(f"{code}\t{entry['filename']}")
    return "\n".join(lines) + "\n"


def plan_export(sections, *, require_approved: bool = True) -> ExportPlan:
    """What would be written, and what stands in the way.

    `sections` are (section_code, title, status, applicability, content,
    table_keys, justification_only) tuples from the router; the last element is
    optional and defaults to False. The gate is here rather than in the writer
    so the UI can show exactly what it would refuse before anybody presses
    anything.

    `justification_only` is load-bearing rather than bookkeeping. A section
    marked not applicable normally exports the sentence saying so, and there is
    nothing in that to approve. But the router hands such a section its DRAFT
    whenever one exists and is non-empty -- and a draft is model-written prose.
    Skipping the approval check on applicability alone therefore exported
    unreviewed AI text under a heading nobody expected to contain any.
    """
    plan = ExportPlan()
    for row in sections:
        code, title, status, applicability, content, table_keys = row[:6]
        justification_only = bool(row[6]) if len(row) > 6 else False
        content = content or ""

        if applicability in ("not_applicable", "referenced_dmf") and justification_only:
            # Still a section of the dossier, with its justification as its
            # body: a numbered heading that simply vanished would read as an
            # omission rather than an answer. Nobody drafted it, so there is
            # nothing here to have approved.
            plan.sections.append(SectionRender(
                section_code=code, title=title, level=heading_level(code),
                content=content, table_keys=[]))
            continue
        if require_approved and status != "approved":
            plan.blockers.append({
                "code": "SECTION_NOT_APPROVED", "section_code": code,
                "message": f"{code} {title} is {status.replace('_', ' ')}, not approved.",
            })
        if not content.strip():
            plan.warnings.append({
                "code": "SECTION_EMPTY", "section_code": code,
                "message": f"{code} {title} has no content.",
            })

        # The section's declared table has to be IN the section. `table_keys`
        # was recorded here from the first version of this module and then read
        # by nothing, so a data section whose [TABLE: ...] line had been edited
        # away -- by a regeneration, or by somebody tidying the draft -- simply
        # exported without its table. No blocker, no warning, no gap in the
        # prose to notice: a specification section with no specification in it,
        # which is the failure this whole module is arranged to prevent.
        present = set(TABLE_MARKER_RE.findall(content))
        for key in table_keys or []:
            if key not in present:
                plan.blockers.append({
                    "code": "TABLE_MISSING", "section_code": code,
                    "message": (f"{code} {title} is a data section whose table is "
                                f"[TABLE: {key}], but its text does not contain that "
                                "marker, so the table would be missing from the export."),
                })

        plan.sections.append(SectionRender(
            section_code=code, title=title, level=heading_level(code),
            content=content, table_keys=list(table_keys or [])))

    for section in plan.sections:
        leaf = leaf_for(section.section_code)
        entry = plan.leaves.setdefault(
            leaf, {"filename": f"{LEAF_NAMES.get(leaf, leaf.lower())}.docx", "sections": []})
        entry["sections"].append(section.section_code)
    return plan


def leaf_filename(leaf: str) -> str:
    return f"{LEAF_NAMES.get(leaf, leaf.lower().replace('.', ''))}.docx"


def ectd_path(base_dir: str, leaf: str) -> str:
    """Where a leaf file goes in a tree mirroring the CTD.

    m3/32-body-data/32s-drug-sub/... is the conventional shape; a publishing
    tool will rearrange it, and a reviewer opening the folder should still be
    able to see what is what.
    """
    branch = "32s-drug-sub" if leaf.startswith("S") else \
        "32p-drug-prod" if leaf.startswith("P") else \
        "32a-app" if leaf.startswith("A") else \
        "32r-reg-info" if leaf.startswith("R") else "other"
    return os.path.join(base_dir, "m3", "32-body-data", branch, leaf_filename(leaf))
