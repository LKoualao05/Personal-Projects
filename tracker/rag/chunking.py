"""Split job descriptions into overlapping, paragraph-aware chunks."""

from __future__ import annotations

import re


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    """Fixed-size windows that break on whitespace."""
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            space = text.rfind(" ", start + max_chars // 2, end)
            end = space if space > 0 else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [w for w in out if w]


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """Pack paragraphs into chunks of at most ``max_chars``.

    Paragraphs longer than ``max_chars`` are split into overlapping windows;
    consecutive chunks share ``overlap`` trailing characters so requirements
    that straddle a boundary stay retrievable.
    """
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*[-•*·]\s)", text) if p.strip()]

    pieces: list[str] = []
    for p in paragraphs:
        pieces.extend(_windows(p, max_chars, overlap) if len(p) > max_chars else [p])

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = current[-overlap:]
        tail = tail[tail.find(" ") + 1:] if " " in tail else tail
        current = f"{tail}\n{piece}" if overlap and len(tail) + len(piece) + 1 <= max_chars else piece
    if current:
        chunks.append(current)
    return chunks
