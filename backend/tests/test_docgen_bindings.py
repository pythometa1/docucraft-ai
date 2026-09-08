"""What each module tells the shared engine, and what the engine does with it.

The shared engine (`app.docgen`) has defaults, and those defaults are today's
clinical values. That makes a forgotten binding invisible: if the CSR ingester
stopped passing its page-local types, TLF chunks would silently start spanning
page breaks, every citation into them would point a reviewer at the wrong
page, and the draft would still read perfectly. No existing test can see it --
the unit tests call the engine directly and get its defaults, and the
end-to-end fixtures are four lines long, far too small to span a page.

So these tests assert the WIRING rather than the behaviour: what the adapter
hands the engine, and what a real ingest produces end to end. They are the
price of the shared engine, and they are cheaper than the defect.
"""

import app.csr.ingest as csr_ingest
from app.docgen.chunking import chunk_extraction
from app.docgen.extraction import ExtractedPage, ExtractedTable, Extraction
from app.docgen.ranking import format_extracts, in_id_range


class _Chunk:
    """The duck type `format_extracts` reads. Not a CsrChunk: the point is that
    the shared function never learns what a CSR is."""

    def __init__(self, **kw):
        self.id = kw.get("id", "c1")
        self.document_id = kw.get("document_id", "d1")
        self.doc_type = kw.get("doc_type", "protocol")
        self.page = kw.get("page")
        self.table_id = kw.get("table_id")
        self.is_table = kw.get("is_table", False)
        self.content = kw.get("content", "text")


# ------------------------------------------------------- the CSR bindings

def test_csr_ingest_binds_its_own_chunking_policy(monkeypatch):
    """The engine's defaults happen to match CSR's values today. This asserts
    the adapter passes them anyway, so a future change to either side is a
    test failure rather than a page number that quietly drifts."""
    seen = {}

    def spy(extraction, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(csr_ingest, "chunk_extraction", spy)
    monkeypatch.setattr(csr_ingest, "extract",
                        lambda *a, **k: Extraction(pages=[], tables=[], page_count=0))

    class _Doc:
        id = "doc-1"
        org_id = "org-1"
        csr_project_id = "proj-1"
        doc_type = "tlf"
        storage_path = "csr/proj-1/x.txt"
        mime_type = "text/plain"
        processing_status = "queued"
        error_message = None
        page_count = None
        chunk_count = 0

    doc = _Doc()

    class _Session:
        def get(self, _model, _id):
            return doc

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(csr_ingest, "SessionLocal", lambda: _Session())
    csr_ingest.ingest_document("doc-1")

    assert seen["page_local_types"] == ("tlf", "narrative")
    assert seen["table_label"] == "Table"


def test_csr_format_extracts_binds_the_style_reference_warning():
    """A prior CSR is retrievable and never citable. The warning is the only
    thing enforcing that, so the binding is asserted rather than assumed."""
    from app.csr.retrieval import format_extracts as csr_format_extracts

    text, source_map = csr_format_extracts([
        _Chunk(id="a", doc_type="prior_csr", content="house style"),
        _Chunk(id="b", doc_type="protocol", content="the study"),
    ])
    assert "STYLE REFERENCE ONLY, never cite as fact" in text.split("\n\n")[0]
    assert "STYLE REFERENCE ONLY" not in text.split("\n\n")[1]
    assert [entry["marker"] for entry in source_map] == ["S1", "S2"]


def test_the_shared_formatter_warns_about_nobody_by_default():
    """Unbound, the engine labels nothing a style reference -- a module that
    forgets to name its own gets no warning rather than the clinical one."""
    text, _ = format_extracts([_Chunk(doc_type="prior_csr")])
    assert "STYLE REFERENCE ONLY" not in text


# ------------------------------------------------------- engine behaviour

def test_page_local_types_actually_change_the_chunking():
    """The binding is only worth asserting because it changes the output.
    A page-local type keeps page 1 and page 2 in separate chunks; without it
    they merge, and the page recorded on the merged chunk is page 1."""
    extraction = Extraction(
        pages=[ExtractedPage(page=1, text="Alpha findings on the first page."),
               ExtractedPage(page=2, text="Beta findings on the second page.")],
        tables=[], page_count=2)

    local = chunk_extraction(extraction, doc_type="tlf", page_local_types=("tlf",))
    merged = chunk_extraction(extraction, doc_type="tlf", page_local_types=())

    assert [c["page"] for c in local] == [1, 2]
    assert len(merged) == 1
    assert "Alpha" in merged[0]["content"] and "Beta" in merged[0]["content"]


def test_the_table_label_is_the_callers_word():
    extraction = Extraction(
        pages=[], page_count=0,
        tables=[ExtractedTable(table_id="3.2.P.5.1", title="Specification",
                               page=None, rows=[["Test", "Limit"], ["Assay", "98.0-102.0 %"]])])
    clinical = chunk_extraction(extraction, doc_type="tlf")
    quality = chunk_extraction(extraction, doc_type="spec_dp", table_label="Specification")

    assert clinical[0]["content"].startswith("Table 3.2.P.5.1 -- Specification")
    assert quality[0]["content"].startswith("Specification 3.2.P.5.1 -- Specification")
    # The values themselves are untouched by the label.
    assert "98.0-102.0 %" in clinical[0]["content"]
    assert "98.0-102.0 %" in quality[0]["content"]


def test_id_ranges_match_on_the_segment_boundary():
    """14.10 is not inside 14.1. A boost given to the wrong table is a wrong
    table quoted in the report."""
    assert in_id_range("14.1.1", "14.1") is True
    assert in_id_range("14.1", "14.1") is True
    assert in_id_range("14.10", "14.1") is False
    assert in_id_range(None, "14.1") is False
