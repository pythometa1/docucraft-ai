"""Putting a stored value into a table cell without touching it on the way.

Flow B, and the reason it is deterministic Python rather than a prompt. A model
may describe what a batch analysis contains; it may never typeset one, because
typesetting a number means deciding how that number is written, and how a
number is written is a claim about the method that produced it.
`app.cmc.values` argues that at length: "0.050" and "0.05" are the same
quantity and not the same reported result.

This module is where that rule either holds or quietly stops holding, so it is
arranged so that breaking it takes a deliberate edit:

* One method turns a stored result into a cell -- `_Tally.cell` -- and it has
  exactly two outcomes: `value_text` character for character, or a hole. There
  is no third branch. Nothing appends `unit` to a value, nothing calls
  `float()`, and nothing reaches `app.generation.value_format`, whose
  `format_value` rounds a number-typed value at three decimal places through a
  float and would print "0.050 %" as "0.05 %".
* `value_numeric` is read in one place only -- choosing which two rows bound an
  observed range -- and even there what is printed is those two rows' own
  strings.

A hole is rendered rather than closed up. A result nobody recorded is "-" in
the cell AND a line in `.missing`, because the failure worth preventing is a
batch analysis table that reads as complete to a reviewer because an empty
column silently collapsed. An unverified value is the mirror image: it is
rendered, counted in `.unverified` and named in `.notes`, never dropped,
because whether unverified data may leave the building is the export gate's
decision and not the renderer's. `include_unverified=False` is that gate
asking for the strict rendering, and then those cells become holes like any
other.

Order is fixed, and it is computed in Python rather than left to `ORDER BY`.
Two exports of unchanged data must render identically, so that a diff between
them means somebody edited the data -- and SQL cannot promise that across
engines, because SQLite's collation in a test and PostgreSQL's in production
disagree about case. The scoping stays in SQL, where it belongs: every builder
filters on org_id and cmc_project_id before it sees a row.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import docx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.generation.reproducibility import normalise_docx
from app.models import (
    CmcBatch, CmcBatchFormula, CmcMaterial, CmcResult, CmcSite, CmcTest,
)
from app.templates import blueprint as bp
from app.templates.emit_docx import emit

#: What an absent value prints as. One character, the same one in every table,
#: so a reader scanning a column sees the gap -- and a matching `.missing`
#: entry names the cell, because a dash in a printed document names nothing.
HOLE = "-"

#: The table style a rendered CTD table is given. `emit` writes no table style
#: at all, and a specification with no ruling is a page of floating numbers.
TABLE_STYLE = "Table Grid"

#: What makes a test an impurity, matched case-insensitively as a substring of
#: `test_name`. `cmc_tests` carries no impurity flag, so the alternative to a
#: name test is listing every test on file -- which puts Description and Assay
#: in a table headed "Impurities". The list is deliberately narrow and
#: `.notes` says out loud that the selection was made by name, because a
#: reviewer confirming the list is short work and an impurity missing from a
#: dossier is not.
_IMPURITY_MARKERS = (
    "impurit", "related substance", "related compound", "degradation product",
    "degradant", "residual solvent", "elemental", "nitrosamine", "genotoxic",
)


class TableError(Exception):
    """A table cannot be rendered as asked."""


class TableUnavailable(TableError):
    """This project holds no data for this table.

    Distinct from a table with holes in it. A hole is rendered, because the
    reviewer needs to see which cell is empty; nothing at all is refused,
    because a grid of dashes under a heading reads as a finding rather than as
    an absence of data, and the caller can say "no data yet" far better than
    this module can.
    """


class UnknownTable(TableError):
    """No builder is registered under this key.

    Kept apart from `TableUnavailable` on purpose. A section marked
    `[TABLE: spec_tabel]` is a typo, and reporting it as "no data for this
    project" sends somebody to look at the data review grid for a row that was
    never the problem.
    """


class RaggedTable(TableError):
    """A builder produced rows of unequal width.

    `emit` refuses a ragged grid -- Word has no representation for one -- and
    it refuses it after the whole document has been assembled. Catching it at
    the builder names the table that is wrong.
    """


@dataclass
class RenderedTable:
    """One table, ready to place, and everything the caller must decide about.

    `rows` and `blocks` say the same thing twice on purpose: `rows` is the grid
    as strings, for QC and for tests to assert against, and `blocks` is that
    grid as blueprint blocks for the emitter. Both come from the same computed
    cells, so they cannot drift.

    `groups` is the third view, and it exists because the first two can differ
    in SHAPE even while agreeing on every value: a stability summary is one
    flat union grid in `rows` and one grid per batch and condition in `blocks`.
    A preview drawn from `rows` would then show a reviewer a table the document
    does not contain. `groups` is the document's own arrangement -- one entry
    per grid, in order -- so a screen that renders it shows what will be
    written.
    """

    key: str
    title: str
    columns: list
    rows: list
    blocks: list
    notes: list = field(default_factory=list)
    unverified: int = 0
    missing: list = field(default_factory=list)
    groups: list = field(default_factory=list)


@dataclass(frozen=True)
class _Scope:
    """Everything a builder is allowed to see. Frozen because a builder that
    could widen its own scope is a builder that can cross a tenant."""

    db: Session
    cmc_project_id: str
    org_id: str
    material_id: str | None = None
    deliverable_id: str | None = None
    include_unverified: bool = True


@dataclass
class _Tally:
    """The bookkeeping every builder shares, and the single place a stored
    result becomes a cell.

    Both halves matter. A second function that decided what a cell says is how
    a unit gets appended in one table and not in another, and how two tables in
    the same dossier come to disagree about what a certificate reported.
    """

    include_unverified: bool
    unverified: int = 0
    missing: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def cell(self, result, label: str) -> str:
        """`result.value_text`, verbatim, or a hole that is also recorded."""
        if result is None:
            self.missing.append(f"{label}: no result recorded")
            return HOLE
        if not result.value_text:
            # A row with an empty value_text is a row with no value in it. It
            # is a hole rather than a blank cell because `emit` drops an empty
            # run, and a cell that renders as nothing is indistinguishable from
            # a cell nobody was asked about.
            self.missing.append(f"{label}: the stored result is empty")
            return HOLE
        if result.verified_by is None:
            if not self.include_unverified:
                self.missing.append(f"{label}: result has not been verified")
                return HOLE
            self.unverified += 1
            self.notes.append(
                f"{label}: {result.value_text} has not been verified by a person")
        return result.value_text


# ---- cells and blocks ----

def _text(value) -> str:
    """A stored string as a cell, or a hole where there is no string.

    The only transformation in the module, and it is not a transformation of a
    value: `None` and `""` are not values.
    """
    return value if value else HOLE


def _cell(text: str, *, bold: bool = False) -> list:
    """One table cell: a list of blocks, holding one paragraph, holding one
    static segment. Static because a placeholder is a fill slot the compiler
    would try to write into, and a rendered value is already the answer."""
    return [bp.paragraph([bp.segment("static", text, bold=bold)])]


def _heading(text: str) -> dict:
    return bp.paragraph([bp.segment("static", text, bold=True)])


def _grid(columns, rows) -> dict:
    header = [_cell(_text(name), bold=True) for name in columns]
    return bp.table([header] + [[_cell(value) for value in row] for row in rows])


def _rendered(key, title, columns, rows, tally, blocks=None, groups=None) -> RenderedTable:
    """The one exit from every builder, so every table is checked the same way.

    The width check is here rather than at emit time because `emit` refuses a
    ragged grid only once a whole document has been assembled, and the message
    it can give names neither the table nor the row.
    """
    for index, row in enumerate(rows):
        if len(row) != len(columns):
            raise RaggedTable(
                f"{key}: row {index} has {len(row)} cell(s) and the table has "
                f"{len(columns)} column(s); Word has no representation for a ragged grid")
    # One grid unless a builder says otherwise: for every table but the
    # stability summary the document is exactly this grid, so the default
    # keeps the two views identical without each builder restating it.
    if groups is None:
        groups = [{"title": title, "columns": list(columns),
                   "rows": [list(r) for r in rows]}]
    for group in groups:
        for index, row in enumerate(group["rows"]):
            if len(row) != len(group["columns"]):
                raise RaggedTable(
                    f"{key}: group {group.get('title')!r} row {index} has {len(row)} "
                    f"cell(s) and {len(group['columns'])} column(s)")
    return RenderedTable(
        key=key, title=title, columns=list(columns), rows=[list(r) for r in rows],
        blocks=list(blocks) if blocks is not None else [_heading(title), _grid(columns, rows)],
        notes=tally.notes, unverified=tally.unverified, missing=tally.missing,
        groups=groups)


# ---- scoped reads ----

def _material(scope: _Scope):
    """The material this table is about, or None when the caller did not scope
    to one. A material id from another project is refused rather than ignored:
    silently rendering the whole project's data under one material's heading is
    how a drug product specification acquires the substance's tests."""
    if not scope.material_id:
        return None
    material = scope.db.scalars(select(CmcMaterial).where(
        CmcMaterial.org_id == scope.org_id,
        CmcMaterial.cmc_project_id == scope.cmc_project_id,
        CmcMaterial.id == scope.material_id,
        CmcMaterial.deleted_at.is_(None))).first()
    if material is None:
        raise TableUnavailable(
            f"material {scope.material_id} is not a live material of project "
            f"{scope.cmc_project_id}")
    return material


def _titled(base: str, material) -> str:
    return f"{base} -- {material.name}" if material is not None else base


def _live_material_ids(scope: _Scope) -> set:
    """Materials that have not been removed.

    A material is soft-deleted, and its tests, batches and results are not --
    they carry no `deleted_at` of their own. Without this filter a
    specification table would keep listing the tests of a substance somebody
    removed from the dossier, which is a limit printed for a material the
    submission no longer claims. Deleting a material retires what it governs;
    the rows survive for audit and stop being rendered.
    """
    return {m.id for m in scope.db.scalars(select(CmcMaterial).where(
        CmcMaterial.org_id == scope.org_id,
        CmcMaterial.cmc_project_id == scope.cmc_project_id,
        CmcMaterial.deleted_at.is_(None))).all()}


def _tests(scope: _Scope) -> list:
    query = select(CmcTest).where(
        CmcTest.org_id == scope.org_id,
        CmcTest.cmc_project_id == scope.cmc_project_id)
    if scope.material_id:
        query = query.where(CmcTest.material_id == scope.material_id)
    live = _live_material_ids(scope)
    rows = [t for t in scope.db.scalars(query).all() if t.material_id in live]
    return sorted(rows, key=lambda t: (t.sort_order or 0, t.test_name or "", t.id))


def _batches(scope: _Scope) -> list:
    query = select(CmcBatch).where(
        CmcBatch.org_id == scope.org_id,
        CmcBatch.cmc_project_id == scope.cmc_project_id)
    if scope.material_id:
        query = query.where(CmcBatch.material_id == scope.material_id)
    live = _live_material_ids(scope)
    rows = [b for b in scope.db.scalars(query).all() if b.material_id in live]
    return sorted(rows, key=lambda b: (b.batch_number or "", b.id))


def _results(scope: _Scope, *, release: bool, test_ids, batch_ids) -> list:
    """The results this table may show, scoped in SQL and then narrowed to the
    tests and batches already in scope -- which is what keeps a material's
    table free of another material's numbers.

    A release result is one with neither a storage condition nor a timepoint;
    that pair of nulls is the store's own definition of "not on stability", so
    it is asked of the database rather than reconstructed here.
    """
    query = select(CmcResult).where(
        CmcResult.org_id == scope.org_id,
        CmcResult.cmc_project_id == scope.cmc_project_id)
    query = (query.where(CmcResult.storage_condition.is_(None),
                         CmcResult.timepoint_months.is_(None))
             if release else query.where(CmcResult.timepoint_months.is_not(None)))
    rows = scope.db.scalars(query).all()
    keep_tests, keep_batches = set(test_ids), set(batch_ids)
    return [r for r in rows if r.test_id in keep_tests and r.batch_id in keep_batches]


def _timeless(scope: _Scope, *, test_ids, batch_ids) -> list:
    """Results that name a storage condition and no timepoint.

    Neither query above returns one: a release result is defined by having
    neither of the two, and a stability result by having a timepoint, so a row
    carrying a condition alone falls between them. It is a real stored value --
    a stability table transcribed without its timepoint header leaves exactly
    this behind, and `app.cmc.qc` reports it as "timepoint not recorded" -- so
    it is read here to be named. A value the store holds, QC raises and no
    table mentions is the silent drop this module exists to refuse.
    """
    rows = scope.db.scalars(select(CmcResult).where(
        CmcResult.org_id == scope.org_id,
        CmcResult.cmc_project_id == scope.cmc_project_id,
        CmcResult.storage_condition.is_not(None),
        CmcResult.timepoint_months.is_(None))).all()
    keep_tests, keep_batches = set(test_ids), set(batch_ids)
    return [r for r in rows if r.test_id in keep_tests and r.batch_id in keep_batches]


def _grouped(rows, key) -> dict:
    out: dict = {}
    for row in rows:
        out.setdefault(key(row), []).append(row)
    return out


def _pick(candidates: list):
    """Which of several results for one cell is rendered.

    A verified row wins over an unverified one, and the id breaks any remaining
    tie so the answer never depends on the order the database returned. The
    caller notes that a choice was made -- two results for one cell is a
    conflict a person has to resolve, and a table that shows one of them
    without saying so has resolved it on their behalf.
    """
    return sorted(candidates, key=lambda r: (r.verified_by is None, r.id))[0]


def _timepoint(value) -> Decimal:
    return Decimal(str(value))


def _timepoint_label(value: Decimal) -> str:
    """A timepoint as a column header.

    This is a schedule label, not a reported result: `timepoint_months` is a
    protocol's own number rather than something an instrument measured, so
    writing 3.0 as "3" costs no significant figure anybody claimed. `normalize`
    drops the trailing zero and `"f"` keeps the answer out of exponent form --
    `Decimal("100.0").normalize()` is `1E+2`, and a column headed "1E+2 months"
    is not a stability table.
    """
    return f"{format(value.normalize(), 'f')} months"


# ---- builders ----

def _spec_table(scope: _Scope) -> RenderedTable:
    """Test, method and acceptance criterion, in the specification's own order.

    Every test on file is listed. Filtering to one stage would be the obvious
    tidy-up and it would silently drop the shelf-life limits from a table
    somebody signs, so where more than one stage is present this says so
    instead.
    """
    # The material is resolved first so that a material id from another project
    # is reported as what it is, rather than as "this project records no tests"
    # -- which sends somebody to look at data that is sitting right there.
    material = _material(scope)
    tests = _tests(scope)
    if not tests:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no tests, so a specification table "
            "would state acceptance criteria nobody set")

    tally = _Tally(scope.include_unverified)
    rows = []
    for test in tests:
        if not test.method_id:
            tally.missing.append(f"{test.test_name}: no method identifier")
        if not test.acceptance_criterion_text:
            tally.missing.append(f"{test.test_name}: no acceptance criterion")
        rows.append([_text(test.test_name), _text(test.method_id),
                     _text(test.acceptance_criterion_text)])

    stages = sorted({t.stage for t in tests if t.stage})
    if len(stages) > 1:
        tally.notes.append(
            "this table lists every test on file and they span more than one stage ("
            + ", ".join(stages) + "); a specification distinguishes them, so confirm the "
            "table is the one this section needs")

    return _rendered("spec_table", _titled("Specification", material),
                     ["Test", "Method", "Acceptance criterion"], rows, tally)


def _batch_analyses(scope: _Scope) -> RenderedTable:
    """Tests down the side, batches across the top, release results only.

    The acceptance criterion sits in the first data column because the table is
    read across: a reviewer wants the limit beside the numbers it judges, not
    on a different page in the specification.
    """
    material = _material(scope)
    tests = _tests(scope)
    batches = _batches(scope)
    if not tests or not batches:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} has "
            f"{len(tests)} test(s) and {len(batches)} batch(es) in scope; a batch analysis "
            "table needs both")

    results = _results(scope, release=True, test_ids=[t.id for t in tests],
                       batch_ids=[b.id for b in batches])
    if not results:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no release results (a result with no "
            "storage condition and no timepoint) for the tests and batches in scope")

    tally = _Tally(scope.include_unverified)
    by_cell = _grouped(results, lambda r: (r.test_id, r.batch_id))
    names = {b.id: b.batch_number for b in batches}

    rows = []
    for test in tests:
        row = [_text(test.test_name), _text(test.acceptance_criterion_text)]
        for batch in batches:
            label = f"{test.test_name} / batch {batch.batch_number}"
            candidates = by_cell.get((test.id, batch.id))
            if candidates and len(candidates) > 1:
                tally.notes.append(
                    f"{label}: {len(candidates)} results are stored for this one cell and one "
                    "of them is shown; the conflict needs resolving in the data review grid")
            row.append(tally.cell(_pick(candidates) if candidates else None, label))
        rows.append(row)

    columns = ["Test", "Acceptance criterion"] + [_text(names[b.id]) for b in batches]
    return _rendered("batch_analyses", _titled("Batch Analyses", material),
                     columns, rows, tally)


def _stability_groups(scope: _Scope):
    """`(tests, batches_by_id, groups, timepoints, results_by_cell, tally)`.

    Shared by the two stability builders so that the summary and the
    completeness matrix cannot disagree about which timepoints exist -- which
    is the one thing a completeness view must never be wrong about.
    """
    tests = _tests(scope)
    batches = _batches(scope)
    test_ids = [t.id for t in tests]
    batch_ids = [b.id for b in batches]
    results = _results(scope, release=False, test_ids=test_ids, batch_ids=batch_ids)
    by_id = {b.id: b for b in batches}
    # Counted per batch and condition, like the unfiled rows below and for the
    # same reason: one line names the gap, one line per result buries it.
    timeless = _grouped(_timeless(scope, test_ids=test_ids, batch_ids=batch_ids),
                        lambda r: (r.batch_id, r.storage_condition or ""))
    if not results:
        # The count goes in the refusal because "no stability results" sends a
        # reader to re-extract a study that is already in the store, when what
        # the store is missing is the timepoint column of it.
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no stability results (a result with a "
            "timepoint) for the tests and batches in scope"
            + (f"; {sum(len(rows) for rows in timeless.values())} result(s) do name a "
               "storage condition but record no timepoint" if timeless else ""))

    tally = _Tally(scope.include_unverified)
    for group in sorted(timeless, key=lambda g: (by_id[g[0]].batch_number or "", g[1], g[0])):
        tally.missing.append(
            f"batch {by_id[group[0]].batch_number} / {group[1]}: {len(timeless[group])} "
            "result(s) record no timepoint, so no column can hold them")
    conditions = {}
    unfiled = {}
    for result in results:
        if not result.storage_condition:
            # A timepoint with no condition is a real row that cannot be filed
            # under a study arm. Grouping it under a hole keeps it visible;
            # dropping it would shrink a completeness view silently. Reported
            # once per batch and timepoint rather than once per result, so a
            # study with twelve tests does not bury the rest of `.missing`.
            unfiled[(result.batch_id, _timepoint(result.timepoint_months))] = True
        conditions[(result.batch_id, result.storage_condition or "")] = True
    for batch_id, point in sorted(unfiled, key=lambda u: (by_id[u[0]].batch_number or "", u[1])):
        tally.missing.append(
            f"batch {by_id[batch_id].batch_number}: results at {_timepoint_label(point)} "
            "record no storage condition")

    groups = sorted(conditions, key=lambda g: (by_id[g[0]].batch_number or "", g[1], g[0]))
    timepoints = sorted({_timepoint(r.timepoint_months) for r in results})
    used = {r.test_id for r in results}
    return ([t for t in tests if t.id in used], by_id, groups, timepoints,
            _grouped(results, lambda r: (r.batch_id, r.storage_condition or "",
                                         _timepoint(r.timepoint_months), r.test_id)),
            tally)


def _stability_summary(scope: _Scope) -> RenderedTable:
    """One block per batch and storage condition: tests down, timepoints across.

    Timepoints are ordered by their number, which is the whole reason they are
    sorted as `Decimal` and not as the strings they will be printed as: sorted
    lexically, a 12-month column lands between 0 and 3 and the table reads as a
    stability study that ran backwards.

    Every block carries the same columns -- the union of timepoints across the
    study -- so the blocks stack into one comparable grid and a condition that
    was never pulled at 12 months shows a hole there rather than a short row
    nobody notices.
    """
    material = _material(scope)
    tests, by_id, groups, timepoints, by_cell, tally = _stability_groups(scope)

    cells: dict = {}
    for batch_id, condition in groups:
        for test in tests:
            for point in timepoints:
                label = (f"{test.test_name} / batch {by_id[batch_id].batch_number} / "
                         f"{condition or HOLE} / {_timepoint_label(point)}")
                candidates = by_cell.get((batch_id, condition, point, test.id))
                if candidates and len(candidates) > 1:
                    tally.notes.append(
                        f"{label}: {len(candidates)} results are stored for this one cell and "
                        "one of them is shown; the conflict needs resolving in the data "
                        "review grid")
                cells[(batch_id, condition, test.id, point)] = tally.cell(
                    _pick(candidates) if candidates else None, label)

    headers = [_timepoint_label(p) for p in timepoints]
    rows, blocks = [], [_heading(_titled("Stability Summary", material))]
    # The document is one grid per batch and condition; `rows` flattens them
    # into a union for QC. `grids` records the document's own arrangement so a
    # preview shows the tables that will actually be written.
    grids = []
    for batch_id, condition in groups:
        block_rows = []
        for test in tests:
            values = [cells[(batch_id, condition, test.id, p)] for p in timepoints]
            rows.append([_text(by_id[batch_id].batch_number), _text(condition),
                         _text(test.test_name)] + values)
            block_rows.append([_text(test.test_name)] + values)
        heading = f"Batch {_text(by_id[batch_id].batch_number)} -- {_text(condition)}"
        blocks.append(_heading(heading))
        blocks.append(_grid(["Test"] + headers, block_rows))
        grids.append({"title": heading, "columns": ["Test"] + headers,
                      "rows": block_rows})

    columns = ["Batch", "Storage condition", "Test"] + headers
    return _rendered("stability_summary", _titled("Stability Summary", material),
                     columns, rows, tally, blocks=blocks, groups=grids)


def _stability_matrix(scope: _Scope) -> RenderedTable:
    """How many results exist at each timepoint, so a hole is visible as a hole.

    The completeness view the QC pass warns on. Its cells are counts of stored
    rows rather than reported values -- the one place in this module where a
    number is produced rather than repeated -- so it says so in `.notes` and a
    count of nothing renders as a hole rather than as a confident zero.
    """
    material = _material(scope)
    tests, by_id, groups, timepoints, by_cell, tally = _stability_groups(scope)
    tally.notes.append(
        "cells are counts of stored results, not reported values; this table is a "
        "completeness view and never a source of data")
    if not scope.include_unverified:
        tally.notes.append("unverified results are not counted")

    rows = []
    for batch_id, condition in groups:
        row = [_text(by_id[batch_id].batch_number), _text(condition)]
        for point in timepoints:
            present = [r for test in tests
                       for r in by_cell.get((batch_id, condition, point, test.id), ())]
            counted = [r for r in present
                       if scope.include_unverified or r.verified_by is not None]
            unverified = sum(1 for r in counted if r.verified_by is None)
            if unverified:
                tally.unverified += unverified
                tally.notes.append(
                    f"batch {by_id[batch_id].batch_number} / {condition or HOLE} / "
                    f"{_timepoint_label(point)}: {unverified} of the {len(counted)} results "
                    "counted here have not been verified by a person")
            if not counted:
                tally.missing.append(
                    f"batch {by_id[batch_id].batch_number} / {condition or HOLE} / "
                    f"{_timepoint_label(point)}: no results")
                row.append(HOLE)
            else:
                row.append(str(len(counted)))
        rows.append(row)

    columns = ["Batch", "Storage condition"] + [_timepoint_label(p) for p in timepoints]
    return _rendered("stability_matrix", _titled("Stability Data Completeness", material),
                     columns, rows, tally)


def _quantity(quantity, unit) -> str:
    """A quantity and its unit as one cell.

    Two stored strings joined by one space. Neither is re-read and neither is
    re-printed, so the digits in the cell are the digits in the row -- the
    alternative, dropping `unit`, is a batch formula whose components have no
    dimension.
    """
    if not quantity:
        return HOLE
    return f"{quantity} {unit}" if unit else quantity


def _formula_rows(scope: _Scope, tally: _Tally, *, full: bool):
    """Rows for the two formula tables.

    A row extracted before any deliverable existed carries no deliverable id
    and belongs to the project, so it is included whichever deliverable is
    being written; excluding it would silently empty the table of a project
    whose data was loaded first and structured second.
    """
    query = select(CmcBatchFormula).where(
        CmcBatchFormula.org_id == scope.org_id,
        CmcBatchFormula.cmc_project_id == scope.cmc_project_id)
    if scope.deliverable_id:
        query = query.where(
            (CmcBatchFormula.cmc_deliverable_id == scope.deliverable_id)
            | (CmcBatchFormula.cmc_deliverable_id.is_(None)))
    components = sorted(scope.db.scalars(query).all(),
                        key=lambda c: (c.sort_order or 0, c.component_name or "", c.id))
    if not components:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no batch formula components")

    rows = []
    for component in components:
        label = component.component_name or component.id
        # The quantities are the data on this row; the component's name and
        # function are how a reader knows which row is being withheld. Blanking
        # the whole row instead would leave a table nobody can read against the
        # unverified data it is hiding.
        withheld = component.verified_by is None and not scope.include_unverified
        if component.verified_by is None:
            if withheld:
                tally.missing.append(f"{label}: quantities have not been verified")
            else:
                tally.unverified += 1
                tally.notes.append(
                    f"{label}: quantities have not been verified by a person")
        row = [
            _text(component.component_name),
            _text(component.function),
            HOLE if withheld else _quantity(component.quantity_per_unit, component.unit),
            HOLE if withheld else _text(component.percent_ww),
        ]
        if full:
            row += [HOLE if withheld else _text(component.quantity_per_batch),
                    _text(component.reference_to_standard)]
        rows.append(row)
    return rows


def _batch_formula(scope: _Scope) -> RenderedTable:
    """The full batch formula: what goes in, how much, and against what standard."""
    tally = _Tally(scope.include_unverified)
    rows = _formula_rows(scope, tally, full=True)
    return _rendered("batch_formula", "Batch Formula",
                     ["Component", "Function", "Quantity per unit", "% w/w",
                      "Quantity per batch", "Reference to standard"], rows, tally)


def _composition_table(scope: _Scope) -> RenderedTable:
    """The 3.2.P.1 subset of the batch formula: the unit dose, not the batch.

    Same rows, four columns. Built from the same source rather than from its
    own query, because a composition that disagreed with the batch formula in
    the same dossier is exactly the defect a deterministic renderer exists to
    make impossible.
    """
    tally = _Tally(scope.include_unverified)
    rows = _formula_rows(scope, tally, full=False)
    return _rendered("composition_table", "Composition",
                     ["Component", "Function", "Quantity per unit", "% w/w"], rows, tally)


def _site_list(scope: _Scope) -> RenderedTable:
    """Every site that makes or tests the product, with what each one does."""
    sites = sorted(scope.db.scalars(select(CmcSite).where(
        CmcSite.org_id == scope.org_id,
        CmcSite.cmc_project_id == scope.cmc_project_id,
        CmcSite.deleted_at.is_(None))).all(), key=lambda s: (s.name or "", s.id))
    if not sites:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no sites")

    tally = _Tally(scope.include_unverified)
    rows = []
    for site in sites:
        # An address and an identifier are what makes a site inspectable, and a
        # 3.2.S.2.1 table without them is a list of company names.
        for column, value in (("address", site.address), ("identifier", site.identifier),
                              ("activities", site.activities)):
            if not value:
                tally.missing.append(f"{site.name}: no {column}")
        rows.append([
            _text(site.name), _text(site.address), _text(site.identifier),
            _text(", ".join(str(a) for a in site.activities or ())),
        ])
    return _rendered("site_list", "Manufacturing and Testing Sites",
                     ["Site", "Address", "Identifier", "Activities"], rows, tally)


def _impurity_table(scope: _Scope) -> RenderedTable:
    """Each impurity, its limit, and the range actually observed across batches.

    The range is two stored strings with a hyphen between them -- the value_text
    of the lowest result and of the highest, chosen by `value_numeric` and then
    printed as they were reported. A recomputed "0.02 - 0.12" would be a number
    no certificate contains, and it is the exact recomputation that loses the
    significant figures the method claimed.
    """
    material = _material(scope)
    tests = [t for t in _tests(scope) if _is_impurity(t.test_name)]
    if not tests:
        raise TableUnavailable(
            f"project {scope.cmc_project_id} records no test whose name identifies it as an "
            "impurity")

    batches = _batches(scope)
    results = _results(scope, release=True, test_ids=[t.id for t in tests],
                       batch_ids=[b.id for b in batches])
    tally = _Tally(scope.include_unverified)
    tally.notes.append(
        "rows were selected by test name because the data store carries no impurity flag; "
        "confirm no impurity is missing from this table")

    by_test = _grouped(results, lambda r: r.test_id)
    numbers = {b.id: b.batch_number or "" for b in batches}
    rows = []
    for test in tests:
        if not test.acceptance_criterion_text:
            tally.missing.append(f"{test.test_name}: no acceptance criterion")
        rows.append([_text(test.test_name), _text(test.acceptance_criterion_text),
                     _observed_range(by_test.get(test.id, []), test, numbers, tally,
                                     include_unverified=scope.include_unverified)])
    return _rendered("impurity_table", _titled("Impurities", material),
                     ["Impurity", "Acceptance criterion", "Observed range across batches"],
                     rows, tally)


def _is_impurity(test_name: str | None) -> bool:
    lowered = (test_name or "").lower()
    return any(marker in lowered for marker in _IMPURITY_MARKERS)


def _observed_range(results, test, batch_numbers, tally: _Tally,
                    *, include_unverified: bool) -> str:
    """The observed range cell, built only out of strings somebody reported."""
    usable = [r for r in results if include_unverified or r.verified_by is not None]
    withheld = len(results) - len(usable)
    if withheld:
        tally.missing.append(
            f"{test.test_name}: {withheld} result(s) are not verified and are not in the range")

    numeric = [r for r in usable if r.value_numeric is not None]
    if not numeric:
        # "ND" across every batch is a real observed range and must survive.
        # Results that disagree and cannot be ordered are not a range at all,
        # and picking one of them would be inventing the answer.
        reported = {r.value_text for r in usable if r.value_text}
        if len(reported) == 1:
            return reported.pop()
        if usable:
            tally.missing.append(
                f"{test.test_name}: no result carries a number, so no range can be stated")
        else:
            tally.missing.append(
                f"{test.test_name}: no result is available for any batch, so no range can "
                "be stated")
        return HOLE

    ordered = sorted(numeric, key=lambda r: (Decimal(str(r.value_numeric)),
                                             batch_numbers.get(r.batch_id, ""), r.id))
    low, high = ordered[0], ordered[-1]
    if len(numeric) != len(usable):
        tally.notes.append(
            f"{test.test_name}: {len(usable) - len(numeric)} result(s) carry no number and "
            "are outside the range shown")

    # Both ends go through the same door every other cell does, so that the one
    # method able to print a result stays the only one -- and so that the two
    # values a range actually shows are the two counted as unverified, rather
    # than every result that went into choosing them.
    ends = [tally.cell(row, f"{test.test_name} / batch {batch_numbers.get(row.batch_id, '')}")
            for row in (low, high)]
    if HOLE in ends:
        return HOLE
    return ends[0] if ends[0] == ends[1] else f"{ends[0]} - {ends[1]}"


#: The table a `[TABLE: key]` marker resolves to. A dict rather than a chain of
#: branches so that a deliverable can only ask for a table somebody wrote.
BUILDERS = {
    "spec_table": _spec_table,
    "batch_analyses": _batch_analyses,
    "stability_summary": _stability_summary,
    "stability_matrix": _stability_matrix,
    "batch_formula": _batch_formula,
    "site_list": _site_list,
    "impurity_table": _impurity_table,
    "composition_table": _composition_table,
}


def render_table(db: Session, *, cmc_project_id: str, org_id: str, table_key: str,
                 material_id: str | None = None, deliverable_id: str | None = None,
                 include_unverified: bool = True) -> RenderedTable:
    """The table under `table_key`, built from this project's stored data.

    `org_id` is required rather than derived from the project row, so that a
    caller cannot render one tenant's numbers by passing another tenant's
    project id and having this module look up whatever it finds.
    """
    builder = BUILDERS.get(table_key)
    if builder is None:
        raise UnknownTable(
            f"no table builder is registered as {table_key!r}; known keys are "
            f"{sorted(BUILDERS)}")
    return builder(_Scope(
        db=db, cmc_project_id=cmc_project_id, org_id=org_id, material_id=material_id,
        deliverable_id=deliverable_id, include_unverified=include_unverified))


def render_to_docx(rendered: RenderedTable, output_path: str) -> str:
    """Write one rendered table to `output_path` and return the path.

    Ruling is applied after `emit`, not instead of it. `emit` writes no
    `tblStyle` of its own -- it builds the grid the pre-scanner reads back and
    nothing else -- so a table emitted straight out of it prints as columns of
    numbers with no lines between them, which for a specification is a page a
    reviewer cannot read across. Setting the style afterwards means `emit` has
    already round-tripped the file it wrote and proved the text in it is the
    text in the body; a style applied before that check would be verified, and
    a style applied by a second renderer would be a second renderer.

    Reopening with python-docx is safe here in a way it is not for a customer's
    file: this document was written by python-docx moments ago, so there are no
    parts of somebody else's package for the round trip to drop. The archive is
    normalised again on the way out, because emitting the same data twice has
    to give the same bytes.
    """
    body = bp.normalise_body({"blocks": list(rendered.blocks), "sect_pr_from": None})
    emit(body, output_path)

    document = docx.Document(output_path)
    for table in document.tables:
        table.style = TABLE_STYLE
    document.save(output_path)
    return normalise_docx(output_path)
