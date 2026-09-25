"""VertexRagRetriever + rag_sync helpers — offline, with a fake RAG client (SDD D3, FR-5.3/5.4)."""

from __future__ import annotations

import pytest

from app import config
from app.policy import retriever as r
from app.policy.ingest import load_corpus
from app.policy.rag_sync import render, write_tfvar


class FakeRag:
    def __init__(self, results=None, exc: Exception | None = None) -> None:
        self.results = results or []
        self.exc = exc
        self.calls: list[tuple[str, int]] = []

    def query(self, text: str, top_k: int):
        self.calls.append((text, top_k))
        if self.exc:
            raise self.exc
        return self.results


@pytest.fixture(scope="module")
def chunks():
    return load_corpus().chunks


def _uri(c) -> str:
    return f"gs://bucket/{load_corpus().corpus_version}/{r.chunk_object_name(c)}"


def test_maps_source_uri_back_to_chunk_with_citation(chunks):
    c = next(c for c in chunks if c.authority == "primary")
    ret = r.VertexRagRetriever("projects/p/locations/asia-southeast1/ragCorpora/1", client=FakeRag([(_uri(c), 0.2)]))
    hits = ret.search("bereavement leave")
    assert [h.chunk.anchor for h in hits] == [c.anchor]
    assert hits[0].to_dict()["citation"] == c.citation_label
    assert hits[0].score == pytest.approx(0.8 + r.VertexRagRetriever.PRIMARY_TIEBREAK)
    assert ret.corpus_version == load_corpus().corpus_version


def test_display_name_source_also_resolves(chunks):
    c = chunks[0]
    ret = r.VertexRagRetriever("c", client=FakeRag([(r.chunk_object_name(c), 0.1)]))
    assert ret.search("anything")[0].chunk is c


def test_distance_threshold_unknown_files_and_dedupe(chunks):
    a, b = chunks[0], chunks[1]
    fake = FakeRag([
        (_uri(a), 0.1), (_uri(a), 0.15),                       # duplicate -> once
        (_uri(b), config.RAG_DISTANCE_THRESHOLD + 0.01),       # too far -> insufficient
        ("gs://bucket/old/stale-anchor.md", 0.05),             # not in current corpus
    ])
    hits = r.VertexRagRetriever("c", client=fake).search("q", top_k=3)
    assert [h.chunk.anchor for h in hits] == [a.anchor]
    assert fake.calls[0][1] == 6  # over-fetch for filtering


def test_jurisdiction_filter(chunks):
    sg = next(c for c in chunks if c.jurisdiction == "SG")
    glob = next(c for c in chunks if c.jurisdiction == "GLOBAL")
    fake = FakeRag([(_uri(sg), 0.1), (_uri(glob), 0.2)])
    hits = r.VertexRagRetriever("c", client=fake).search("leave", jurisdiction="MY")
    assert [h.chunk.jurisdiction for h in hits] == ["GLOBAL"]


def test_primary_outranks_summary_at_equal_distance(chunks):
    summ = next(c for c in chunks if c.authority == "summary")
    prim = next(c for c in chunks if c.authority == "primary")
    fake = FakeRag([(_uri(summ), 0.3), (_uri(prim), 0.3)])
    hits = r.VertexRagRetriever("c", client=fake).search("sick leave")
    assert hits[0].chunk is prim


def test_closer_summary_beats_farther_primary(chunks):
    """Regression (live eval): a x1.2 primary boost pushed correct summary chunks (e.g. §2.2)
    out of the top-k. Primary may only break near-ties, never override a clearly closer hit."""
    summ = next(c for c in chunks if c.authority == "summary")
    prim = next(c for c in chunks if c.authority == "primary")
    fake = FakeRag([(_uri(prim), 0.30), (_uri(summ), 0.20)])
    hits = r.VertexRagRetriever("c", client=fake).search("baby bonding email delegation")
    assert [h.chunk for h in hits] == [summ, prim]


def test_query_sent_verbatim():
    """Dense embeddings handle paraphrase; BM25 synonym expansion is not appended (it cost MRR)."""
    fake = FakeRag([])
    r.VertexRagRetriever("c", client=fake).search("  funeral leave  ")
    assert fake.calls[0][0] == "funeral leave"


def test_threshold_default_calibrated():
    """Farthest relevant top hit on the labelled set is 0.53; off-topic starts ~0.50-0.65."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if k != "RAG_DISTANCE_THRESHOLD"}
    out = subprocess.run([sys.executable, "-c", "from app import config; print(config.RAG_DISTANCE_THRESHOLD)"],
                         env=env, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                         check=True).stdout
    assert 0.53 < float(out) <= 0.55


def test_last_error_is_per_thread():
    import threading

    ret = r.VertexRagRetriever("c", client=FakeRag(exc=RuntimeError("503 unavailable")))
    ret.search("vacation")
    seen: list[str | None] = []
    t = threading.Thread(target=lambda: seen.append(ret.last_error))
    t.start()
    t.join()
    assert ret.last_error and seen == [None]


def test_rag_error_fails_to_refusal_not_guess():
    ret = r.VertexRagRetriever("c", client=FakeRag(exc=RuntimeError("503 unavailable")))
    assert ret.search("vacation") == []
    assert ret.last_error and "503" in ret.last_error
    ret._client = FakeRag([])
    ret.search("vacation")
    assert ret.last_error is None


def test_blank_query_short_circuits():
    fake = FakeRag([])
    assert r.VertexRagRetriever("c", client=fake).search("   ") == []
    assert fake.calls == []


def test_get_retriever_selects_by_config(monkeypatch):
    r.get_retriever.cache_clear()
    monkeypatch.setattr(config, "RAG_CORPUS", "projects/p/locations/l/ragCorpora/9")
    try:
        assert isinstance(r.get_retriever(), r.VertexRagRetriever)
    finally:
        r.get_retriever.cache_clear()
        monkeypatch.setattr(config, "RAG_CORPUS", "")
    assert isinstance(r.get_retriever(), r.LocalBM25Retriever)
    r.get_retriever.cache_clear()


def test_project_parsed_from_corpus_name():
    assert r._project_from_corpus("projects/jm-01-project/locations/x/ragCorpora/1") == "jm-01-project"
    assert r._project_from_corpus("bare") is None


def test_render_prepends_parent_context(chunks):
    c = chunks[5]
    body = render(c)
    assert body.startswith(f"# {c.citation_label}\n")
    assert c.text in body and f"Jurisdiction: {c.jurisdiction}" in body


def test_write_tfvar_inserts_then_replaces(tmp_path):
    f = tmp_path / "terraform.tfvars"
    f.write_text('project_id = "p"\n')
    write_tfvar(f, "rag_corpus", "projects/p/locations/l/ragCorpora/1")
    write_tfvar(f, "rag_corpus", "projects/p/locations/l/ragCorpora/2")
    text = f.read_text()
    assert text.count("rag_corpus") == 1 and 'ragCorpora/2"' in text and 'project_id = "p"' in text
