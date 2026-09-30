"""Read-only audit for approved customer-facing knowledge files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from grannyflat_bot.config import KNOWLEDGE_DIR
from grannyflat_bot.knowledge.ingest import chunk_markdown, load_markdown_files


TOPIC_MARKERS: dict[str, tuple[str, ...]] = {
    "product_specifications": ("60 m", "70 m", "80 m"),
    "floor_plans": ("a60", "b60", "a70", "a80"),
    "pricing_policy": ("price", "pricing", "报价"),
    "installation_process": ("installation", "install", "安装"),
    "australian_standards": ("class 1a", "occupancy certificate", "building approval"),
    "faq": ("frequently", "faq", "common questions"),
    "shipping": ("shipping", "container", "sea", "海运"),
    "builder_process": ("licensed local builder", "builder", "建造商"),
    "qhc_information": ("qhc",),
}


def audit_knowledge(directory: Path | None = None) -> dict[str, Any]:
    root = directory or KNOWLEDGE_DIR
    files = load_markdown_files(root)
    errors: list[str] = []
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    corpus_parts: list[str] = []

    if not files:
        errors.append("没有可用的客户知识文件")

    for path in files:
        text = path.read_text(encoding="utf-8")
        corpus_parts.append(text.lower())
        chunks = chunk_markdown(path)
        if not text.strip():
            errors.append(f"空知识文件：{path.name}")
        if len(text) > 250_000:
            warnings.append(f"知识文件过大，建议拆分：{path.name}")
        sources.append(
            {
                "source": path.relative_to(root).as_posix(),
                "bytes": len(text.encode("utf-8")),
                "chunks": len(chunks),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )

    corpus = "\n".join(corpus_parts)
    coverage: dict[str, bool] = {}
    for topic, markers in TOPIC_MARKERS.items():
        present = any(marker in corpus for marker in markers)
        coverage[topic] = present
        if not present:
            warnings.append(f"缺少已批准资料主题：{topic}")

    forbidden_names = {"cooperation_and_costs.md", "rules.yaml"}
    leaked = [item["source"] for item in sources if Path(item["source"]).name in forbidden_names]
    if leaked:
        errors.append("内部成本资料进入客户知识库：" + ", ".join(leaked))

    return {
        "ok": not errors,
        "root": str(root),
        "source_count": len(sources),
        "chunk_count": sum(item["chunks"] for item in sources),
        "sources": sources,
        "coverage": coverage,
        "warnings": warnings,
        "errors": errors,
    }
