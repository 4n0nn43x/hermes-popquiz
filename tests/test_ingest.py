"""Ingest tests: txt, md, html, pdf via anydoc (real one skipped when missing, fake one injected),
too large, outside roots, symlink escape, empty, truncation."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from popquiz_plugin.ingest import html_to_text, load_document
from popquiz_plugin.model import GameError


def err(code: str):
    return pytest.raises(GameError, match=rf"^{code}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


def test_txt(root):
    f = root / "notes.txt"
    f.write_bytes(b"\xef\xbb\xbfPlain text\r\nsecond line \xff")
    doc = load_document(str(f), [root])
    assert doc.kind == "txt" and doc.title == "notes"
    assert doc.text.startswith("Plain text\nsecond line") and "\ufffd" in doc.text
    assert doc.sha256 == hashlib.sha256(f.read_bytes()).hexdigest()
    assert doc.path == str(f.resolve()) and doc.truncated is False


def test_md_title_from_heading(root):
    f = root / "runbook.md"
    f.write_text("Intro line\n\n# **Payments** Runbook\n\nBody.", encoding="utf-8")
    doc = load_document(str(f), [root])
    assert doc.kind == "md" and doc.title == "Payments Runbook"


def test_no_extension_is_text(root):
    f = root / "README"
    f.write_text("hello world", encoding="utf-8")
    assert load_document(str(f), [root]).kind == "txt"


def test_html_drops_script_and_style(root):
    f = root / "page.html"
    f.write_text("<html><head><title>Ops Guide</title><style>body{}</style></head><body>"
                 "<h1>Main</h1><p>Keep logs 30&nbsp;days.</p><script>alert('x')</script><p>Second</p></body></html>",
                 encoding="utf-8")
    doc = load_document(str(f), [root])
    assert doc.kind == "html" and doc.title == "Ops Guide"
    assert "alert" not in doc.text and "body{}" not in doc.text
    assert "Keep logs 30 days." in doc.text and "Second" in doc.text


def test_html_title_falls_back_to_h1():
    title, text = html_to_text("<h1>Heading <b>One</b></h1><div>a</div><div>b</div>")
    assert title == "Heading One" and text.splitlines()[-1] == "b"


def test_outside_roots(root, tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("secret", encoding="utf-8")
    with err("path_not_allowed"):
        load_document(str(f), [root])


def test_dotdot_escape(root, tmp_path):
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    with err("path_not_allowed"):
        load_document(str(root / ".." / "secret.txt"), [root])


def test_symlink_escape(root, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(target)
    with err("path_not_allowed"):
        load_document(str(link), [root])


def test_symlink_inside_root_ok(root):
    target = root / "real.txt"
    target.write_text("inside text", encoding="utf-8")
    (root / "alias.txt").symlink_to(target)
    assert load_document(str(root / "alias.txt"), [root]).text == "inside text"


def test_outside_and_missing_is_not_allowed(root, tmp_path):
    with err("path_not_allowed"):
        load_document(str(tmp_path / "nope.txt"), [root])


@pytest.mark.parametrize("name", ["missing.txt", ""])
def test_not_found(root, name):
    with err("not_found"):
        load_document(str(root / name) if name else "", [root])


def test_directory_is_not_found(root):
    (root / "sub").mkdir()
    with err("not_found"):
        load_document(str(root / "sub"), [root])


def test_too_large(root):
    f = root / "big.txt"
    f.write_text("x" * 2000, encoding="utf-8")
    with err("too_large"):
        load_document(str(f), [root], max_bytes=1000)


def test_truncated_flag(root):
    f = root / "long.md"
    f.write_text("y" * 500, encoding="utf-8")
    doc = load_document(str(f), [root], max_chars=100)
    assert len(doc.text) == 100 and doc.truncated


def test_empty_document(root):
    f = root / "empty.txt"
    f.write_text("  \n\t ", encoding="utf-8")
    with err("empty_document"):
        load_document(str(f), [root])


def test_unknown_extension(root):
    f = root / "image.png"
    f.write_bytes(b"\x89PNG")
    with err("unsupported_format"):
        load_document(str(f), [root])


def test_multiple_roots(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    f = other / "a.md"
    f.write_text("# A\ntext", encoding="utf-8")
    assert load_document(str(f), [root, other]).title == "A"


# ---------------------------------------------------------------- anydoc formats
@pytest.fixture
def fake_anydoc(monkeypatch):
    mod = types.ModuleType("anydoc")
    calls = []

    class NeedsOcrError(Exception):
        pass

    def to_markdown(path):
        calls.append(path)
        if path.endswith("scan.pdf"):
            raise NeedsOcrError("image only")
        if path.endswith("blank.pdf"):
            return ""
        if path.endswith("broken.pdf"):
            raise RuntimeError("bad xref")
        return "# Extracted Title\n\nPage text with 42 numbers."

    mod.to_markdown = to_markdown
    mod.NeedsOcrError = NeedsOcrError
    monkeypatch.setitem(sys.modules, "anydoc", mod)
    return calls


@pytest.mark.parametrize("ext", [".pdf", ".docx", ".epub"])
def test_anydoc_formats_with_fake(root, fake_anydoc, ext):
    f = root / f"doc{ext}"
    f.write_bytes(b"%binary")
    doc = load_document(str(f), [root])
    assert doc.kind == ext[1:] and doc.title == "Extracted Title" and "42" in doc.text
    assert fake_anydoc == [str(f.resolve())]


def test_scanned_pdf_is_empty_document(root, fake_anydoc):
    f = root / "scan.pdf"
    f.write_bytes(b"%PDF")
    with err("empty_document"):
        load_document(str(f), [root])


def test_blank_pdf_is_empty_document(root, fake_anydoc):
    f = root / "blank.pdf"
    f.write_bytes(b"%PDF")
    with err("empty_document"):
        load_document(str(f), [root])


def test_broken_pdf_extract_failed(root, fake_anydoc):
    f = root / "broken.pdf"
    f.write_bytes(b"%PDF")
    with err("extract_failed"):
        load_document(str(f), [root])


def test_pdf_without_anydoc_is_unsupported(root, monkeypatch):
    monkeypatch.setitem(sys.modules, "anydoc", None)  # import raises ImportError
    f = root / "doc.pdf"
    f.write_bytes(b"%PDF")
    with pytest.raises(GameError) as info:
        load_document(str(f), [root])
    assert info.value.code == "unsupported_format" and ".md" in info.value.detail


@pytest.mark.skipif(importlib.util.find_spec("anydoc") is None, reason="anydoc not installed (Hermes core dep)")
def test_real_pdf_with_anydoc(root):  # pragma: no cover - only where anydoc exists
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
           b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj "
           b"4 0 obj<</Length 44>>stream\nBT /F1 18 Tf 20 60 Td (Keep logs 30 days) Tj ET\nendstream endobj "
           b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")
    f = root / "real.pdf"
    f.write_bytes(pdf)
    try:
        doc = load_document(str(f), [root])
    except GameError as exc:
        pytest.skip(f"anydoc could not parse the minimal PDF: {exc.code}")
    assert "30 days" in doc.text
