"""Grounding tests: corpus ingestion QA (SDD §3.7) and retrieval quality on key handbook facts."""

from __future__ import annotations

import pytest

from app import config
from app.policy.ingest import ingest, load_corpus
from app.policy.retriever import get_retriever, spotlight


def test_corpus_is_the_governed_handbook():
    docs = config.PROJECT_DIR.parent / "docs" / "ALTOSTRAT SINGAPORE EMPLOYEE POLICY HANDBOOK & CONDUCT GUIDELINES.md"
    if docs.exists():
        assert docs.read_bytes() == config.CORPUS_PATH.read_bytes()


def test_ingestion_quality_gates():
    rep = ingest()
    assert rep.corpus_version == load_corpus().corpus_version
    assert len(rep.chunks) > 100
    assert len(rep.quarantined) >= 3                      # C-3 meta-commentary removed
    assert not any("drafted text" in c.text.lower() for c in rep.chunks)
    assert len({c.anchor for c in rep.chunks}) == len(rep.chunks)  # C-4 unique anchors
    topics = {c.semantic_topic for c in rep.chunks}
    assert any("Relocation" in t for t in topics)         # C-1 re-titled misfiled bullets


@pytest.mark.parametrize("query,expect_section", [
    ("bereavement leave for a parent", "3"),
    ("vacation days after 8 years of service", "1.2"),
    ("maximum meal expense per day on business travel", "4.4"),
    ("home office monitor allowance", "5.4"),
    ("relocation allowance cap", "5.5"),
    ("email delegation during planned medical leave", "2.2"),
    ("outpatient sick leave days", "1.1"),
])
def test_retrieval_top_hits(query, expect_section):
    hits = get_retriever().search(query, top_k=5)
    assert hits, query
    sections = [h.chunk.subsection or h.chunk.section_number for h in hits[:3]]
    assert any(s == expect_section or s.startswith(expect_section + ".") or expect_section.startswith(s)
               for s in sections), (query, sections)


def test_out_of_corpus_topic_has_no_supporting_text():
    # BM25 scores are lexical, not a sufficiency oracle: the Policy Agent must judge that
    # these excerpts don't answer the question and refuse (measured by the eval suite).
    hits = get_retriever().search("monthly parking subsidy", top_k=5)
    # The only 'parking' text is "traffic/parking fines ... non-reimbursable" (§4.x) — a near-miss.
    assert not any(k in h.chunk.text.lower() for h in hits for k in ("parking subsidy", "parking allowance"))


def test_spotlight_marks_excerpts_as_data():
    hits = get_retriever().search("bereavement leave", top_k=2)
    s = spotlight(hits)
    assert s.count("<policy_excerpt") == len(hits) and 'citation="' in s
