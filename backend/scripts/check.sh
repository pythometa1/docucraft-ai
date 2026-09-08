#!/usr/bin/env bash
#
# The gate. CI runs this exact script, so "green locally" and "green in CI"
# cannot drift apart into two different definitions of done.
#
# Runs fully offline: no API keys, no network. conftest.py blanks every provider
# key, and any model-backed path is expected to refuse rather than reach out.
#
#   ./scripts/check.sh                  run the gate
#   ./scripts/check.sh --update-goldens accept new golden output (explain it in the PR)

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

# Floors are the *measured* values, not aspirations. Raise them when the number
# goes up; a floor nobody can meet gets commented out within a week.
#
# This number is not comparable to the pre-restructure one. Coverage used to be
# scoped to app/services; the §15 module split dissolved that package, so the
# gate now measures the whole of app/ -- routers, main and bootstrap included.
# Same tests, wider denominator.
COV_FLOOR="${COV_FLOOR:-76}"

echo "==> pytest (offline, --cov-fail-under=${COV_FLOOR})"
"$PYTHON" -m pytest -q \
  --cov=app --cov-report=term --cov-report=xml \
  --cov-fail-under="${COV_FLOOR}" "$@"

echo
echo "==> per-module floors for the document-producing path"
"$PYTHON" - <<'PY'
import pathlib
import sys
import xml.etree.ElementTree as ET

# Keyed on the bare filename, so a module moving between packages does not
# silently drop out of the gate -- the MISSING check below catches that.
FLOORS = {
    # The document-producing path.
    "templates/parsers/docx_prescan.py": 100,
    "generation/docx_renderer.py": 95,
    "compiler/rule_compiler.py": 87,
    "generation/missing_policy.py": 95,
    "generation/renderers.py": 100,
    "generation/resolution_engine.py": 90,
    "generation/batch_runner.py": 88,
    "manifests/validator.py": 95,
    # Authoring. `emit_docx` writes the document a customer opens in Word, and
    # its whole job is to be the exact inverse of `docx_prescan` -- which is why
    # that one is at 100 and this one is beside it. `blueprint.normalise_body`
    # is the guard that makes one segment mean one span; an untested branch
    # there is a manifest whose slots address the span to their left for the
    # rest of a paragraph, which is a letter with a salary in the wrong sentence
    # and every QA gate reporting clean.
    "templates/emit_docx.py": 90,
    "templates/blueprint.py": 92,
    # Reading a legacy template back. `read_docx` decides what a customer sees
    # when they open their own document, and its self-check is the only thing
    # standing between "off by one paragraph" and a manifest that addresses the
    # wrong text for the rest of the file. `lift` is where one bad legacy object
    # is kept from costing the whole template.
    "templates/read_docx.py": 85,
    "templates/lift.py": 90,
    # The gate. Every branch here is a decision about whether a template ships,
    # and the failure mode of a wrong one is not a crash: a false blocker teaches
    # people to click past the gate, and a missing one lets a template through
    # that will produce wrong letters.
    "templates/blueprint_lint.py": 88,
    # Starting from nothing, and rescuing what the old editor produced. Both
    # write documents somebody will send.
    "templates/kits.py": 90,
    "templates/library_migration.py": 90,
    # The one validated way to change a template. Every branch is a guard, and a
    # guard that is not exercised is a guard that is not there -- the co-pilot's
    # safety rests entirely on these refusals holding.
    "templates/blueprint_ops.py": 90,
    # What a model call cost. 100 on pricing because every branch of it is a
    # number that ends up on somebody's invoice, and the failure mode of an
    # untested one is a bill that is quietly wrong rather than an error.
    "llm/pricing.py": 100,
    "llm/metering.py": 95,
    # What the estate produced and what it cost. The failure mode of an untested
    # branch here is a number on a screen that is confidently wrong, which is
    # worse than a blank one.
    "analytics.py": 92,
    # What state a document is in. Every branch is a decision about whether a
    # letter can be signed, and the two bugs that made the module necessary were
    # both a status written from one caller's local knowledge rather than derived
    # -- so an untested branch here is a document approved over an open objection.
    "generation/document_status.py": 95,
    # The two wirings that turned a library into a path: nothing wrote to the
    # embeddings table, and nothing called the overlay renderer.
    "retrieval/indexing.py": 94,
    "generation/pdf_fill.py": 95,
    # The invoice service. `invoicing` is the Decimal money math -- every branch
    # is a figure on somebody's tax return; `numbering` hands out INV-#### and an
    # untested branch there is a duplicate number in an audit; `single` is the
    # shared single-record generation core both the manifest endpoint and the
    # invoice endpoint execute; `blueprint_author` is the only place a model's
    # output becomes a template, and its refusals are the whole safety story.
    "finance/invoicing.py": 85,
    # The clinical service's derived counts: a partial sum on a study report
    # is a wrong number that looks deliberate, so every branch of the
    # all-or-nothing rule is exercised.
    "clinical/derivations.py": 95,
    # The shared document engine. Every module's sources pass through these:
    # a chunking bug does not produce a wrong sentence, it produces a right
    # sentence citing the wrong page, which no reviewer can catch by reading.
    "docgen/chunking.py": 90,
    "docgen/extraction.py": 85,
    "docgen/markers.py": 90,
    "docgen/ranking.py": 88,
    # The CMC module's numeric discipline. `values` is the only thing standing
    # between a certificate of analysis reporting 0.050 and a dossier printing
    # 0.05, and `limits` decides whether a batch conformed -- a wrong verdict
    # there is a conformance claim nobody made. Both fail by looking right.
    "cmc/values.py": 95,
    "cmc/limits.py": 92,
    # Flow B end to end. `tables` is the only thing that writes a laboratory
    # value into a document, and it does so by copying a stored string -- an
    # untested branch there is a number nobody typed. `qc` decides whether a
    # dossier may leave the building, and `export` resolves every [TABLE:]
    # marker at the moment of writing, which is what makes a correction in the
    # grid reach a document nobody regenerated.
    "cmc/tables.py": 90,
    "cmc/qc.py": 88,
    "cmc/export.py": 85,
    "cmc/drafting.py": 88,
    "numbering.py": 88,
    "generation/single.py": 90,
    "compiler/blueprint_author.py": 90,
    # The contract: what production is allowed to execute, and whether a
    # document can still be reproduced from the pair it was generated against.
    "manifests/models.py": 90,
    "manifests/versioning.py": 95,
    "expressions/language.py": 92,
    "compiler/confidence.py": 100,
    # The things that keep one customer's data away from another's, and away
    # from the model provider. A regression here is not a bug report, it is an
    # incident, so these carry the highest floors in the file.
    "authz.py": 95,
    "downloads.py": 100,
    # §16 retention and the deletion cascade. 100% because the thing being
    # asserted is that data is gone: an untested branch here is a row, a blob or
    # an embedding a customer was told had been destroyed.
    "retention.py": 100,
    # Below 100 on purpose. The PostgreSQL-only statements -- set_config, the
    # RLS maintenance bypass -- cannot execute on SQLite, and the suite runs on
    # SQLite. What is covered is every decision the module makes.
    # 87 not 88: the PostgreSQL branches of the scope helpers cannot execute on
    # the SQLite run this floor is measured against. The Postgres suite covers
    # them; see the TEST_DATABASE_URL step in CI.
    "tenancy.py": 87,
    "llm/boundary.py": 95,
    "llm/redaction.py": 88,
    "retrieval/vector.py": 90,
    "retrieval/mapping_memory.py": 90,
}

# Three floors are only reachable with the client-owned template masters, which
# are deliberately not published with this repository -- they are a real offer
# letter and two ICC contracts. `conftest.pytest_collection_modifyitems` skips
# the tests that need them, so on a checkout without those files the coverage of
# the modules that read a .docx is legitimately lower.
#
# CI is exactly such a checkout, so these floors could never be met there. That
# made the gate fail on every machine that does *not* hold customer data, which
# is the wrong way round and is the kind of red that teaches people to stop
# reading CI. They are reported as unmeasurable instead -- visible, and not a
# pass -- and enforced in full wherever the masters are present.
FIXTURE_DEPENDENT = {
    "compiler/rule_compiler.py",
    "generation/docx_renderer.py",
    "templates/parsers/docx_prescan.py",
}
FIXTURES = pathlib.Path("tests/fixtures")
missing_masters = [
    name for name in ("templates/hospira_offer.docx",
                      "templates/icc_ct036_template.docx",
                      "templates/icc_ct040_template.docx")
    if not (FIXTURES / name).exists()
]

root = ET.parse("coverage.xml").getroot()
rates = {
    cls.get("filename"): round(float(cls.get("line-rate")) * 100)
    for cls in root.iter("class")
}

failed = False
for path, floor in sorted(FLOORS.items()):
    actual = rates.get(path)
    if actual is None:
        print(f"  MISSING {path} -- not in the coverage report")
        failed = True
        continue
    if actual < floor and missing_masters and path in FIXTURE_DEPENDENT:
        print(f"  --  {path:28} {actual:3d}%  (floor {floor}% -- not measurable "
              "without the client masters)")
        continue
    mark = "ok " if actual >= floor else "LOW"
    print(f"  {mark} {path:28} {actual:3d}%  (floor {floor}%)")
    if actual < floor:
        failed = True

if missing_masters:
    print()
    print("  Note: " + str(len(missing_masters)) + " client template master(s) absent, so the "
          "tests that read a real .docx were skipped.")
    print("  The floors marked -- above are enforced wherever those files are present.")

sys.exit(1 if failed else 0)
PY

echo
echo "All checks passed."
