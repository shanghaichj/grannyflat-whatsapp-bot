from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from grannyflat_bot.config import KNOWLEDGE_DIR


@dataclass
class Chunk:
    id: str
    source: str
    text: str


def load_markdown_files(directory: Path | None = None) -> list[Path]:
    root = directory or KNOWLEDGE_DIR
    if not root.exists():
        return []
    return sorted(
        path for path in root.glob("**/*.md")
        if path.name != "cooperation_and_costs.md"
        and "internal" not in path.relative_to(root).parts
    )


def chunk_markdown(path: Path, *, max_chars: int = 900) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    # Split on headings first, then pack into windows
    parts = re.split(r"(?m)(?=^#{1,3}\s)", text)
    chunks: list[Chunk] = []
    buffer = ""
    idx = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if buffer and len(buffer) + len(part) + 2 > max_chars:
            chunks.append(
                Chunk(id=f"{path.name}:{idx}", source=path.name, text=buffer.strip())
            )
            idx += 1
            buffer = part
        else:
            buffer = f"{buffer}\n\n{part}".strip() if buffer else part
        while len(buffer) > max_chars:
            chunks.append(
                Chunk(
                    id=f"{path.name}:{idx}",
                    source=path.name,
                    text=buffer[:max_chars].strip(),
                )
            )
            idx += 1
            buffer = buffer[max_chars:].strip()
    if buffer.strip():
        chunks.append(
            Chunk(id=f"{path.name}:{idx}", source=path.name, text=buffer.strip())
        )
    return chunks


def ingest_knowledge(directory: Path | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in load_markdown_files(directory):
        chunks.extend(chunk_markdown(path))
    return chunks


def ingest_learned_scripts() -> list[Chunk]:
    """客服自动收录的话术，参与检索。"""
    from grannyflat_bot.agent_learn import learned_scripts_path

    path = learned_scripts_path()
    if not path.exists():
        return []
    return chunk_markdown(path)
