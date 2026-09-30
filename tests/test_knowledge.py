import os

os.environ["MOCK_LLM"] = "1"

from grannyflat_bot.config import get_settings
from grannyflat_bot.knowledge.ingest import ingest_knowledge
from grannyflat_bot.knowledge.audit import audit_knowledge
from grannyflat_bot.knowledge.retriever import KnowledgeRetriever
from grannyflat_bot.llm.client import LLMClient


def test_ingest_has_chunks():
    chunks = ingest_knowledge()
    assert len(chunks) >= 3
    sources = {c.source for c in chunks}
    assert "faq.md" in sources


def test_retrieve_relevant():
    get_settings.cache_clear()
    retriever = KnowledgeRetriever.build(LLMClient())
    hits = retriever.retrieve("Do I need council approval for a granny flat?", top_k=3)
    assert hits
    joined = " ".join(h.text.lower() for h in hits)
    assert "council" in joined


def test_context_bundle_includes_internal_source_trace():
    get_settings.cache_clear()
    retriever = KnowledgeRetriever.build(LLMClient())

    bundle = retriever.context_bundle("Class 1a building approval")

    assert bundle["context"]
    assert bundle["sources"]
    assert bundle["confidence"] in {"low", "medium", "high"}
    assert all("source" in item and "score" in item for item in bundle["sources"])


def test_empty_query_returns_no_knowledge():
    get_settings.cache_clear()
    retriever = KnowledgeRetriever.build(LLMClient())

    assert retriever.context_bundle("   ") == {
        "context": "",
        "sources": [],
        "confidence": "none",
    }


def test_knowledge_audit_excludes_internal_cost_file():
    report = audit_knowledge()

    assert report["ok"] is True
    assert report["source_count"] >= 3
    assert all(
        item["source"] != "cooperation_and_costs.md"
        for item in report["sources"]
    )
    assert report["coverage"]["australian_standards"] is True
