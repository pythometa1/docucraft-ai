"""Store-and-parse for uploaded template blobs.

Single upload and bulk onboarding both create a TemplateFile + TemplateVersion,
but only the single-upload path parsed the document into its section tree. The
bulk path left `section_count` at 0 with no TemplateSection rows, so every
bulk-onboarded template was structurally invisible: the mapping wizard showed an
empty section list and the coverage endpoint reported nothing to map.

Since bulk onboarding is the path an estate of thousands of templates actually
arrives through, that gap mattered more than the single-file path it was missing
from. Both now go through here.
"""

import dataclasses

from sqlalchemy.orm import Session

from app.metrics import TEMPLATE_PARSE, timed
from app.models import TemplateFile, TemplateSection, TemplateVersion
from app.templates.parsers.docx_parser import parse_docx_template
from app.public_errors import public_message
from app.storage import abs_path


def parse_template_version(db: Session, tf: TemplateFile, tv: TemplateVersion) -> None:
    """Parse `tv`'s stored blob and record the outcome on both rows.

    Never raises: a template that cannot be parsed is still a template the user
    uploaded, so it is kept with `status="failed"` and a readable `parse_error`
    rather than disappearing or aborting a bulk run partway through.
    """
    # An immutable PDF is not parsed into headings and runs -- §6 addresses it by
    # page geometry, and §12 renders it by overlaying approved regions. So the
    # onboarding step is to derive the region inventory a reviewer will confirm,
    # not to look for a document structure that is not there.
    if str(tv.blob_path).lower().endswith(".pdf"):
        try:
            from app.generation.pdf_renderer import scan_page_regions

            with timed(db, org_id=tv.org_id, operation=TEMPLATE_PARSE):
                regions = scan_page_regions(str(abs_path(tv.blob_path)))
            tv.page_regions = [dataclasses.asdict(r) for r in regions]
            tv.template_kind = "pdf_overlay"
            tv.section_count = len(tv.page_regions)
            tf.current_version_id = tv.id
            tf.status = "ready"
            tf.parse_error = None
        except Exception as exc:  # noqa: BLE001 - an unreadable upload is kept, not lost
            tf.current_version_id = tv.id
            tf.status = "failed"
            tf.parse_error = public_message(exc, "Could not read this PDF template.")
        return

    try:
        # §18's "template parse and semantic model, p95 < 30 s". The target is a
        # design estimate until something records what the parse actually costs
        # on a real estate, so the parse is timed even though nobody is blocked
        # on it.
        with timed(db, org_id=tv.org_id, operation=TEMPLATE_PARSE):
            parsed = parse_docx_template(str(abs_path(tv.blob_path)))
        tv.template_kind = parsed.kind
        tv.section_count = len(parsed.sections)
        tv.jinja_vars = parsed.jinja_vars
        for section in parsed.sections:
            db.add(TemplateSection(
                template_version_id=tv.id,
                org_id=tv.org_id,
                order_index=section.order_index,
                level=section.level,
                title=section.title,
                section_path=section.section_path,
                anchor={"type": "heading_range", "start_el": section.start_el, "end_el": section.end_el},
                fingerprint=section.fingerprint,
                example_text=section.example_text,
                fillable=section.fillable,
            ))
        tf.current_version_id = tv.id
        tf.status = "ready"
        tf.parse_error = None
    except Exception as exc:
        tf.current_version_id = tv.id  # keep the blob reachable for re-parsing
        tf.status = "failed"
        # The parser's own text names the path it opened on this host.
        tf.parse_error = public_message(
            exc, "Could not parse template. Check that it is a valid Word document.")
