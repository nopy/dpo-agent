"""Ingest — Layer 1 of the 8-layer GraphRAG pipeline.

Parses contracts (PDF, DOCX, HTML, TXT) into a stream of `Chunk`
records. Port of `wiki-contracts/kgpipeline/ingest.py`.

Each chunk has:
  - chunk_id (deterministic hash of source + section + text)
  - source_id (the contract identifier)
  - source_path
  - section_path (e.g. "Section 8.3 → Limitation of Liability → (b)")
  - page_number (when available)
  - text
  - hash (sha256 of the text for de-dup)

This is the "Ingestion Layer" from the GraphRAG build pipeline
pattern. For chunking at legal-meaningful boundaries (sections,
clauses, defined terms) see `contract-clause-chunking-pattern`; this
module implements the simpler "fixed character windows with metadata
preservation" pattern that's sufficient for most contracts.

dpo-agent integration: the kg_extract task uses these chunks as
input. For documents already in dpo-agent's `DocumentTools` store,
the chunks can be derived from the document's chunked content.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Union


@dataclass
class Chunk:
    """A chunk of contract text with provenance metadata."""
    chunk_id: str
    source_id: str
    source_path: Optional[str]
    section_path: str  # e.g. "1. Introduction" or "8.3 Limitation of Liability"
    page_number: Optional[int]
    text: str
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def char_len(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "section_path": self.section_path,
            "page_number": self.page_number,
            "text": self.text,
            "hash": self.hash,
        }


# ─── File-type dispatch ─────────────────────────────────────────────

def parse_file(path: Union[str, Path]) -> List[Chunk]:
    """Parse a contract file into Chunk records. Dispatches on extension."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(p)
    if ext == ".docx":
        return parse_docx(p)
    if ext in (".html", ".htm"):
        return parse_html(p)
    if ext in (".txt", ".md"):
        return parse_text(p)
    raise ValueError(f"Unsupported file type: {ext} ({p})")


def parse_directory(path: Union[str, Path], recursive: bool = True) -> List[Chunk]:
    """Parse all supported files in a directory. Returns a flat list of chunks."""
    p = Path(path)
    if p.is_file():
        return parse_file(p)
    if not p.is_dir():
        raise ValueError(f"Not a file or directory: {p}")
    files: List[Path] = []
    if recursive:
        for ext in (".pdf", ".docx", ".html", ".htm", ".txt", ".md"):
            files.extend(p.rglob(f"*{ext}"))
    else:
        for ext in (".pdf", ".docx", ".html", ".htm", ".txt", ".md"):
            files.extend(p.glob(f"*{ext}"))
    chunks: List[Chunk] = []
    for f in sorted(files):
        try:
            chunks.extend(parse_file(f))
        except Exception as e:
            # Don't fail the whole batch on a single bad file
            print(f"  ! failed to parse {f.name}: {e}")
    return chunks


# ─── Per-format parsers ─────────────────────────────────────────────

def parse_pdf(path: Path) -> List[Chunk]:
    """Parse a PDF into chunks. One chunk per page (or split long pages)."""
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber not installed: pip install pdfplumber") from e
    source_id = path.stem
    chunks: List[Chunk] = []
    with pdfplumber.open(path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            text = _normalize_whitespace(text)
            if not text.strip():
                continue
            for sub_text, sub_idx in _split_long_text(text, max_chars=4000):
                cid = _make_chunk_id(source_id, page_idx, sub_idx, sub_text)
                chunks.append(Chunk(
                    chunk_id=cid,
                    source_id=source_id,
                    source_path=str(path),
                    section_path=f"Page {page_idx}",
                    page_number=page_idx,
                    text=sub_text,
                ))
    return chunks


def parse_docx(path: Path) -> List[Chunk]:
    """Parse a DOCX into chunks. Splits at heading boundaries when possible."""
    try:
        from docx import Document
    except ImportError as e:
        raise RuntimeError("python-docx not installed: pip install python-docx") from e
    source_id = path.stem
    doc = Document(str(path))
    current_section = "Body"
    current_text: list[str] = []
    groups: list[tuple[str, str]] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        is_heading = (
            (para.style and para.style.name and "Heading" in para.style.name)
            or (len(text) < 80 and text == text.upper() and any(c.isalpha() for c in text))
        )
        if is_heading:
            if current_text:
                groups.append((current_section, "\n".join(current_text)))
            current_section = text
            current_text = []
        else:
            current_text.append(text)
    if current_text:
        groups.append((current_section, "\n".join(current_text)))
    chunks: List[Chunk] = []
    for sec_idx, (section, text) in enumerate(groups, start=1):
        text = _normalize_whitespace(text)
        if not text.strip():
            continue
        for sub_text, sub_idx in _split_long_text(text, max_chars=4000):
            cid = _make_chunk_id(source_id, sec_idx, sub_idx, sub_text)
            chunks.append(Chunk(
                chunk_id=cid,
                source_id=source_id,
                source_path=str(path),
                section_path=section,
                page_number=None,
                text=sub_text,
            ))
    return chunks


def parse_html(path: Path) -> List[Chunk]:
    """Parse HTML into chunks. Strips tags, splits at heading boundaries."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("beautifulsoup4 not installed: pip install beautifulsoup4") from e
    source_id = path.stem
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    chunks: List[Chunk] = []
    section_idx = 0
    current_section = "Body"
    current_text: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li"]):
        el_text = el.get_text(" ", strip=True)
        if not el_text:
            continue
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if current_text:
                section_idx += 1
                text = _normalize_whitespace(" ".join(current_text))
                if text.strip():
                    chunks.extend(_emit_chunk(source_id, path, current_section, section_idx, text))
            current_section = el_text
            current_text = []
        else:
            current_text.append(el_text)
    if current_text:
        section_idx += 1
        text = _normalize_whitespace(" ".join(current_text))
        if text.strip():
            chunks.extend(_emit_chunk(source_id, path, current_section, section_idx, text))
    return chunks


def parse_text(path: Path) -> List[Chunk]:
    """Parse a plain text / markdown file. Splits at blank lines."""
    source_id = path.stem
    text = _normalize_whitespace(path.read_text(encoding="utf-8", errors="replace"))
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: List[Chunk] = []
    for para_idx, para in enumerate(paragraphs, start=1):
        para = para.strip()
        if not para:
            continue
        section = "Body"
        if para.startswith("#"):
            first_line = para.split("\n", 1)[0]
            section = first_line.lstrip("#").strip()
        for sub_text, sub_idx in _split_long_text(para, max_chars=4000):
            cid = _make_chunk_id(source_id, para_idx, sub_idx, sub_text)
            chunks.append(Chunk(
                chunk_id=cid,
                source_id=source_id,
                source_path=str(path),
                section_path=section,
                page_number=None,
                text=sub_text,
            ))
    return chunks


# ─── Helpers ───────────────────────────────────────────────────────

def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace: collapse runs, strip lines, keep paragraphs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _split_long_text(text: str, max_chars: int = 4000) -> List[tuple[str, int]]:
    """Split a long text at paragraph boundaries into <= max_chars chunks."""
    if len(text) <= max_chars:
        return [(text, 0)]
    chunks: List[tuple[str, int]] = []
    paragraphs = text.split("\n\n")
    current: list[str] = []
    current_len = 0
    sub_idx = 0
    for para in paragraphs:
        if current_len + len(para) + 2 > max_chars and current:
            chunks.append(("\n\n".join(current), sub_idx))
            sub_idx += 1
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append(("\n\n".join(current), sub_idx))
    return chunks


def _make_chunk_id(source_id: str, section_idx: int, sub_idx: int, text: str) -> str:
    """Deterministic chunk ID: source + section + sub + content hash."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{source_id}__s{section_idx:03d}_u{sub_idx}_{h}"


def _emit_chunk(
    source_id: str,
    path: Path,
    section: str,
    section_idx: int,
    text: str,
) -> List[Chunk]:
    return [
        Chunk(
            chunk_id=_make_chunk_id(source_id, section_idx, sub_idx, sub_text),
            source_id=source_id,
            source_path=str(path),
            section_path=section,
            page_number=None,
            text=sub_text,
        )
        for sub_text, sub_idx in _split_long_text(text, max_chars=4000)
    ]


# ─── Corpus ───────────────────────────────────────────────────────

class Corpus:
    """An iterable collection of chunks with simple stats."""

    def __init__(self, chunks: Iterable[Chunk]) -> None:
        self.chunks: List[Chunk] = list(chunks)

    def __iter__(self):
        return iter(self.chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    def by_source(self, source_id: str) -> List[Chunk]:
        return [c for c in self.chunks if c.source_id == source_id]

    def sources(self) -> List[str]:
        """Return the unique source_ids in this corpus, in first-seen order."""
        seen: list[str] = []
        for c in self.chunks:
            if c.source_id not in seen:
                seen.append(c.source_id)
        return seen

    def total_chars(self) -> int:
        return sum(c.char_len() for c in self.chunks)

    def stats(self) -> dict:
        return {
            "num_chunks": len(self.chunks),
            "num_sources": len(self.sources()),
            "total_chars": self.total_chars(),
            "sources": self.sources(),
        }
