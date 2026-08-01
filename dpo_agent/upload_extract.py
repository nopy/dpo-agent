"""File extraction — turn binary file content into text.

This is a thin layer over `dpo_agent.kg.ingest` that takes
**bytes** (not a path on disk), which is the shape we need
for:

  - In-memory uploads (the FastAPI server reads the
    `UploadFile` into bytes — no temp file needed)
  - Programmatic use (tests can pass synthetic bytes)

For PDF/DOCX the existing `parse_pdf`/`parse_docx` parsers
in `dpo_agent.kg.ingest` take a `Path`. To avoid changing those,
we write the bytes to a `tempfile.NamedTemporaryFile` and
call them through it. The tempfile is closed and unlinked
before this function returns, so there are no leftover files
on disk.

# Format support

- `.txt`, `.md`, `.markdown` — UTF-8 decoded directly
- `.html`, `.htm` — `parse_html` (or `parse_file` dispatch)
- `.pdf` — `pdfplumber` via `parse_pdf`
- `.docx` — `python-docx` via `parse_docx`

If the file extension isn't recognized, raises
`UnsupportedFormatError`. If the parser fails (corrupt file,
missing library), raises `ExtractionError` with the cause.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from .kg.ingest import parse_file


# Extension → format mapping. Lower-cased.
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown",
    ".html", ".htm",
    ".pdf",
    ".docx",
}


class UnsupportedFormatError(ValueError):
    """The uploaded file's extension is not in SUPPORTED_EXTENSIONS."""


class ExtractionError(RuntimeError):
    """Parsing the file failed (corrupt file, missing library, etc.)."""


def extract_text(
    content: bytes,
    filename: str,
    *,
    max_bytes: int = 50 * 1024 * 1024,
) -> str:
    """Extract text from uploaded file content.

    Args:
        content: raw file bytes from the upload.
        filename: original filename (used to choose parser).
        max_bytes: server-side safety cap (default 50 MB).

    Returns:
        Extracted text as a single string. Page/section
        boundaries are preserved with `\n\n---\n\n`
        separators so the dpo-agent's chunks are visible.

    Raises:
        UnsupportedFormatError: file extension not recognized.
        ExtractionError: parser failed (corrupt file, missing
            library, encrypted DOCX, scanned PDF with no text
            layer, etc.). The exception's `__cause__` is the
            original parser exception.
        ValueError: file exceeds max_bytes.
    """
    # Validation
    if len(content) > max_bytes:
        raise ValueError(
            f"File too large: {len(content)} bytes "
            f"(limit is {max_bytes})"
        )

    lower = filename.lower()
    ext = ""
    for candidate in SUPPORTED_EXTENSIONS:
        if lower.endswith(candidate):
            ext = candidate
            break
    if not ext:
        raise UnsupportedFormatError(
            f"Unsupported file extension for {filename!r}. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )

    # Plain text formats: decode directly without going
    # through tempfile/parse_file (which is path-oriented).
    if ext in (".txt", ".md", ".markdown"):
        return content.decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        return _extract_via_tempfile(content, filename)

    # PDF / DOCX: requires tempfile because the underlying
    # libraries open by path.
    return _extract_via_tempfile(content, filename)


def _extract_via_tempfile(content: bytes, filename: str) -> str:
    """Write content to a tempfile, dispatch to parse_file,
    clean up, return concatenated text."""
    with tempfile.NamedTemporaryFile(
        suffix=Path(filename).suffix,
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        try:
            chunks = parse_file(tmp_path)
        except Exception as e:
            raise ExtractionError(
                f"Failed to extract text from {filename!r}: {e}"
            ) from e
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    # Concatenate the chunks, preserving page/section boundaries
    # with horizontal rules so the LLM can see the document
    # structure.
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            parts.append("\n\n---\n\n")
        # section_path is e.g. "Page 3" or "Section 2.1 > 2.1.2".
        if chunk.section_path:
            parts.append(f"<!-- {chunk.section_path} -->\n\n")
        parts.append(chunk.text)
    return "".join(parts)
