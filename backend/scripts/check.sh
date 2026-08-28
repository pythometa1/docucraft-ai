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
    # The two wirings that turned a library into a path: nothing wrote to the
    # embeddings table, and nothing called the overlay renderer.
    "retrieval/indexing.py": 94,
    "generation/pdf_fill.py": 95,
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
    mark = "ok " if actual >= floor else "LOW"
    print(f"  {mark} {path:28} {actual:3d}%  (floor {floor}%)")
    if actual < floor:
        failed = True

sys.exit(1 if failed else 0)
PY

echo
echo "All checks passed."
