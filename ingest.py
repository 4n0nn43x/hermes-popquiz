"""Document ingestion (SPEC section 5): path -> Document, confined to allowed roots.

Stdlib only; ``anydoc`` (a Hermes core dependency) is imported lazily for PDF, DOCX and EPUB.

Choices made where the SPEC is silent:
- Checks run in this order: inside an allowed root (on the fully resolved real path, so a symlink
  escaping a root is ``path_not_allowed``), then ``not_found`` (also for a directory), then
  ``too_large``, then the format. A path outside the roots is refused before revealing whether it exists.
- Unknown extensions: ``unsupported_format``. Anydoc failures: ``NeedsOcrError`` (scanned PDF) and
  empty output -> ``empty_document``; any other extraction error -> ``extract_failed``.
- ``.markdown`` is read as ``md``; text files fall back to replacement characters on bad UTF-8 and a
  leading BOM is dropped.
- HTML title: ``<title>``, else first ``<h1>``, else file stem. Block tags become line breaks.
- ``Document.truncated`` flags a text cut to ``max_chars``; ``sha256`` is over the raw file bytes.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

from .model import Document, GameError

TEXT_EXT = {"": "txt", ".txt": "txt", ".text": "txt", ".md": "md", ".markdown": "md"}
HTML_EXT = {".html": "html", ".htm": "html"}
ANYDOC_EXT = {".pdf": "pdf", ".docx": "docx", ".epub": "epub"}

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd", "hr", "main", "nav"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self.h1 = ""
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP_TAGS:
            self.skip += 1
        if tag == "h1" and not self.h1:
            self._in_h1 = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag == "h1":
            self._in_h1 = False
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self._in_h1:
            self.h1 += data
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) from an HTML string, dropping script/style."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = " ".join(parser.title.split()) or " ".join(parser.h1.split())
    return title, parser.text()


def _inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            real_root = Path(root).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if path == real_root or path.is_relative_to(real_root):
            return True
    return False


def _anydoc_text(path: Path) -> str:
    try:
        import anydoc  # type: ignore[import-not-found]  # lazy: Hermes core dependency
    except ImportError as exc:
        raise GameError("unsupported_format",
                        "PDF, DOCX and EPUB need the 'anydoc' package (firecrawl-anydoc, shipped with Hermes); "
                        "convert the document to .txt or .md instead") from exc
    try:
        return anydoc.to_markdown(str(path)) or ""
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "NeedsOcrError":
            raise GameError("empty_document", "scanned PDF without a text layer (OCR is not supported)") from exc
        raise GameError("extract_failed", f"could not extract text: {type(exc).__name__}") from exc


def load_document(path: str, allowed_roots: list[Path], max_bytes: int = 20_000_000,
                  max_chars: int = 400_000) -> Document:
    if not isinstance(path, str) or not path.strip():
        raise GameError("not_found", "empty path")
    try:
        real = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise GameError("path_not_allowed", "path cannot be resolved") from exc
    if not _inside(real, list(allowed_roots or [])):
        raise GameError("path_not_allowed", "the document must be inside an allowed folder")
    if not real.is_file():
        raise GameError("not_found", "no such file")
    size = real.stat().st_size
    if size > max_bytes:
        raise GameError("too_large", f"{size} bytes, limit {max_bytes}")
    ext = real.suffix.lower()
    raw = real.read_bytes()
    title = ""
    if ext in TEXT_EXT:
        kind = TEXT_EXT[ext]
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    elif ext in HTML_EXT:
        kind = "html"
        title, text = html_to_text(raw.decode("utf-8", errors="replace"))
    elif ext in ANYDOC_EXT:
        kind = ANYDOC_EXT[ext]
        text = _anydoc_text(real)
    else:
        raise GameError("unsupported_format",
                        f"{ext or 'no extension'} is not supported; use txt, md, html, pdf, docx or epub")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise GameError("empty_document", "no text could be extracted")
    heading = _HEADING.search(text)
    if heading and kind != "html":
        title = re.sub(r"[*_`]", "", heading.group(1)).strip()
    title = title or real.stem
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return Document(title=title[:200], text=text, path=str(real), sha256=hashlib.sha256(raw).hexdigest(),
                    kind=kind, truncated=truncated)
