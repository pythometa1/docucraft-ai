"""The PDF conversion must not quietly drop the letter.

A missing font does not make LibreOffice fail. It substitutes, the substitute
has no CJK glyphs, and every ideograph in the document comes out blank or as one
repeated placeholder. Exit code zero, a plausible file size, a PDF that opens --
and a Chinese offer letter reduced to its Latin fragments:

    2026-08-27
    Gao Yan
    Promotion          2026-09-01
    Engineering    Backend Engineer
    29,500.00

Every number correct, every word gone. That is worse than a failed conversion,
because it looks like a finished document.

The guard that was meant to catch this could not, three times over: it keyed on
the document's declared `language` (this letter is Chinese content under a
project tagged `en`), its font list knew only Linux and Windows names (a Mac
with forty-eight CJK fonts reported none), and it only ever produced a *note*,
which the download endpoint discards.
"""

import pathlib

import pytest

from app.generation.pdf_renderer import (
    PreviewUnavailable, _cjk_chars, _refuse_if_script_was_lost,
)


def _docx(path, paragraphs):
    import docx

    d = docx.Document()
    for text in paragraphs:
        d.add_paragraph(text)
    d.save(str(path))
    return path


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


def _fake_pdf(monkeypatch, text):
    """Stand in for the converted PDF, so these tests need no LibreOffice."""
    import app.generation.pdf_renderer as mod

    class _Reader:
        def __init__(self, _path):
            self.pages = [_FakePage(text)]

    monkeypatch.setattr(mod, "PdfReader", _Reader, raising=False)
    import sys
    import types
    fake = types.ModuleType("pypdf")
    fake.PdfReader = _Reader
    monkeypatch.setitem(sys.modules, "pypdf", fake)


# ------------------------------------------------------------------ the detector

def test_cjk_is_recognised_across_the_three_scripts():
    assert _cjk_chars("变动将于生效") == set("变动将于生效")
    assert _cjk_chars("ひらがな")
    assert _cjk_chars("한글")
    # And nothing else is mistaken for it.
    assert _cjk_chars("Backend Engineer 29,500.00 — Shanghai") == set()


# ------------------------------------------------------------------ the refusal

def test_a_conversion_that_ate_the_chinese_is_refused(tmp_path, monkeypatch):
    source = _docx(tmp_path / "letter.docx", [
        "尊敬的Gao Yan，",
        "我谨代表辉瑞中国，向您确认因Promotion而产生的以下变动。",
    ])
    # What LibreOffice actually produced: the Latin survives, every ideograph
    # collapses onto one placeholder glyph.
    _fake_pdf(monkeypatch, "隐隐隐 Gao Yan隐\n隐隐隐隐隐隐 Promotion 隐隐隐隐隐")

    with pytest.raises(PreviewUnavailable) as caught:
        _refuse_if_script_was_lost(source, tmp_path / "out.pdf")

    message = str(caught.value)
    assert "did not survive" in message
    # It says how bad, and it points at the file that is still correct.
    assert "%" in message
    assert ".docx is correct" in message


def test_a_conversion_that_kept_the_chinese_is_accepted(tmp_path, monkeypatch):
    """The check must not simply refuse every CJK document -- on a host with the
    fonts, this is the ordinary path and it has to stay silent."""
    source = _docx(tmp_path / "letter.docx", ["尊敬的Gao Yan，", "变动将于生效。"])
    _fake_pdf(monkeypatch, "尊敬的Gao Yan，\n变动将于生效。")

    _refuse_if_script_was_lost(source, tmp_path / "out.pdf")  # does not raise


def test_a_document_with_no_cjk_is_never_examined(tmp_path, monkeypatch):
    """An English letter cannot lose Chinese it never had, and must not pay for
    a PDF text extraction to establish that."""
    source = _docx(tmp_path / "letter.docx", ["Dear Gao Yan,", "Your new salary is 29,500.00."])

    def _explode(_path):
        raise AssertionError("the PDF must not be read for a document with no CJK")

    import sys
    import types
    fake = types.ModuleType("pypdf")
    fake.PdfReader = _explode
    monkeypatch.setitem(sys.modules, "pypdf", fake)

    _refuse_if_script_was_lost(source, tmp_path / "out.pdf")  # does not raise


def test_a_partial_loss_is_still_a_loss(tmp_path, monkeypatch):
    """Half the characters is not a document anybody can send. The threshold is
    on *distinct* characters, not on length -- tofu substitution preserves the
    count exactly, one wrong glyph per right one, so counting would see nothing
    wrong at all."""
    source = _docx(tmp_path / "letter.docx", ["变动将于二零二六年九月一日生效并确认"])
    _fake_pdf(monkeypatch, "变动将于")

    with pytest.raises(PreviewUnavailable):
        _refuse_if_script_was_lost(source, tmp_path / "out.pdf")


def test_an_unreadable_pdf_is_not_treated_as_proof(tmp_path, monkeypatch):
    """A PDF this cannot parse says nothing about whether the letter survived,
    and refusing on it would block downloads for a reason that is not evidence."""
    source = _docx(tmp_path / "letter.docx", ["变动将于生效。"])

    import sys
    import types

    class _Boom:
        def __init__(self, _path):
            raise ValueError("not a pdf")

    fake = types.ModuleType("pypdf")
    fake.PdfReader = _Boom
    monkeypatch.setitem(sys.modules, "pypdf", fake)

    _refuse_if_script_was_lost(source, tmp_path / "out.pdf")  # does not raise
