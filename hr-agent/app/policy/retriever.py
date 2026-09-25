"""Policy retrieval behind a swappable interface (SDD D3 / §3.7).

`VertexRagRetriever` is the deployed implementation (Vertex AI RAG Engine in
asia-southeast1, selected when `RAG_CORPUS` is set). `LocalBM25Retriever` is the
deterministic, offline, metadata-aware fallback used by tests, eval and local dev.
Both return the same `Hit`s, so the Policy Agent's tool contract does not change.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from rank_bm25 import BM25Okapi

from app import config
from app.policy.ingest import Chunk, load_corpus

_STOP = set(
    "a an the of to for in on at by with and or is are am be do does did i my me we our you your "
    "what how much many can could would should will it this that there their them as from about "
    "any if when which who whom whose get got have has had please tell know".split()
)

# C-5 terminology drift + common employee phrasing → handbook vocabulary.
SYNONYMS: dict[str, list[str]] = {
    "workday": ["workweek"],
    "pto": ["vacation", "leave"],
    "annual": ["vacation"],
    "holiday": ["vacation", "holidays"],
    "mc": ["medical", "certificate"],
    "sick": ["sick", "outpatient"],
    "medical": ["sick", "hospitalization", "medical"],
    "funeral": ["bereavement"],
    "death": ["bereavement"],
    "passed": ["bereavement"],
    "monitor": ["equipment", "monitors", "home", "office"],
    "laptop": ["equipment", "hardware"],
    "wfh": ["remote", "hybrid", "telework"],
    "remote": ["remote", "hybrid", "telework"],
    "relocate": ["relocation", "transferring"],
    "relocation": ["relocation", "transferring", "moving"],
    "transfer": ["relocation", "transferring"],
    "moving": ["relocation"],
    "badge": ["badging", "building", "access"],
    "ticket": ["ticket", "itsm", "servicenow"],
    "meal": ["meal", "meals"],
    "food": ["meal"],
    "maternity": ["maternity", "parental"],
    "paternity": ["baby", "bonding"],
    "father": ["baby", "bonding"],
    "expense": ["expense", "reimbursement", "concur"],
    "reimburse": ["reimbursement", "reimbursable", "expense"],
    "gift": ["gifts", "courtesies"],
    "email": ["email", "delegation"],
    "delegate": ["delegation"],
    "carryover": ["carry", "carried", "over"],
    "harassment": ["harassment"],
    "whistleblow": ["reporting", "concerns"],
}


_SUFFIXES = ("izations", "ization", "ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied",
             "ates", "ate", "ed", "ly")


def _stem(t: str) -> str:
    """Light, deterministic suffix stripping + UK→US spelling (eval RCA v2: vocabulary mismatch).

    Not a full Porter stemmer on purpose: conservative rules keep numbers, codes and
    short words intact so exact figures/citations still dominate BM25 scoring.
    """
    if len(t) <= 4 or not t.isalpha():
        return t
    t = t.replace("isation", "ization").replace("ise", "ize") if t.endswith(("isation", "isations", "ise", "ised",
                                                                           "ising")) else t
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            base = t[: -len(suf)]
            return base + "y" if suf in ("ies", "ied") else base
    if t.endswith(("sses", "xes", "zes", "ches", "shes")) and len(t) > 5:
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 4:
        return t[:-1]
    return t


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9$]+", text.lower()) if t not in _STOP]


def _index_tokens(tokens: list[str]) -> list[str]:
    return [_stem(t) for t in tokens]


def _expand(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for t in tokens:
        stem = t.rstrip("s")
        out.extend(SYNONYMS.get(t, SYNONYMS.get(stem, [])))
    return out


@dataclass
class Hit:
    chunk: Chunk
    score: float

    def to_dict(self) -> dict:
        c = self.chunk
        return {
            "citation": c.citation_label,
            "anchor": c.anchor,
            "section": c.subsection or c.section_number,
            "semantic_topic": c.semantic_topic,
            "jurisdiction": c.jurisdiction,
            "authority": c.authority,
            "relevance_score": round(self.score, 3),
            "text": c.text,
        }


class Retriever(Protocol):
    corpus_version: str

    def search(self, query: str, top_k: int = 5, jurisdiction: str | None = None) -> list[Hit]: ...


def _in_jurisdiction(chunk: Chunk, jurisdiction: str | None) -> bool:
    return not jurisdiction or chunk.jurisdiction in (jurisdiction, "ALL", "GLOBAL")


def _rank(hits: list[Hit], top_k: int) -> list[Hit]:
    """Sort by score, drop identical text (summary/detail overlaps keep the best), cut to top_k."""
    seen: set[str] = set()
    result: list[Hit] = []
    for h in sorted(hits, key=lambda h: h.score, reverse=True):
        if h.chunk.content_hash in seen:
            continue
        seen.add(h.chunk.content_hash)
        result.append(h)
        if len(result) >= top_k:
            break
    return result


class LocalBM25Retriever:
    PRIMARY_BOOST = 1.2
    MIN_SCORE = 2.0  # below this the context is treated as insufficient (FR-5.4)

    def __init__(self) -> None:
        report = load_corpus()
        self.corpus_version = report.corpus_version
        self.chunks = report.chunks
        docs = [
            _index_tokens(_tokens(f"{c.semantic_topic} {c.semantic_topic} {c.subsection_title} {c.text}"))
            for c in self.chunks
        ]
        self._bm25 = BM25Okapi(docs)

    def search(self, query: str, top_k: int = 5, jurisdiction: str | None = None) -> list[Hit]:
        q = _index_tokens(_expand(_tokens(query)))
        if not q:
            return []
        scores = self._bm25.get_scores(q)
        hits = [
            Hit(chunk, float(s) * (self.PRIMARY_BOOST if chunk.authority == "primary" else 1.0))
            for chunk, s in zip(self.chunks, scores, strict=True)
            if _in_jurisdiction(chunk, jurisdiction)
        ]
        return [h for h in _rank(hits, top_k) if h.score >= self.MIN_SCORE]


def chunk_object_name(chunk: Chunk) -> str:
    """GCS object / RAG file name for one chunk. The anchor is the join key back to Chunk."""
    return f"{chunk.anchor}.md"


def _anchor_from_source(uri_or_name: str) -> str:
    return uri_or_name.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md")


class VertexRagRetriever:
    """Vertex AI RAG Engine retriever (asia-southeast1, SDD D3 / CON-5).

    RAG Engine owns embedding + vector search only. Each deterministic chunk from
    `ingest()` is imported as its own file (`<anchor>.md`, see `app.policy.rag_sync`),
    so a retrieved context's `source_uri` maps back to the local `Chunk` — citations
    (FR-5.3), jurisdiction filter (C-6), primary preference (C-2) and de-duplication
    behave as with BM25. On any RAG error we return no hits: the Policy Agent then
    refuses and escalates (§5.4 "Retrieval → Refuse + escalate"), it never guesses.

    Ranking is by embedding distance; `authority=primary` is only a tie-breaker. A
    multiplicative boost demoted correct summary chunks (live eval on the 100 labelled
    questions: Recall@5 97% -> 99%, MRR 0.915 -> ~0.97 once removed). The query is sent
    verbatim: BM25 synonym expansion slightly hurt the dense embedding.
    """

    PRIMARY_TIEBREAK = 0.01

    def __init__(self, corpus: str, client: object | None = None) -> None:
        report = load_corpus()
        self.corpus = corpus
        self.corpus_version = report.corpus_version
        self._by_anchor = {c.anchor: c for c in report.chunks}
        self._client = client
        self._rag = None  # vertexai.rag module, initialised once on first query
        self._init_lock = threading.Lock()
        self._local = threading.local()  # searches run concurrently in worker threads

    @property
    def last_error(self) -> str | None:
        """Error from the most recent search on the *calling* thread (None on success)."""
        return getattr(self._local, "error", None)

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        self._local.error = value

    def _rag_module(self):  # pragma: no cover - network
        with self._init_lock:
            if self._rag is None:
                import vertexai
                from vertexai import rag

                vertexai.init(project=_project_from_corpus(self.corpus), location=config.RAG_LOCATION)
                self._rag = rag
        return self._rag

    def _query(self, text: str, top_k: int) -> list[tuple[str, float]]:
        """Return [(source, distance)] from RAG Engine."""
        if self._client is not None:  # injected fake (unit tests)
            return self._client.query(text, top_k)
        rag = self._rag_module()  # pragma: no cover - network
        resp = rag.retrieval_query(  # pragma: no cover
            text=text,
            rag_resources=[rag.RagResource(rag_corpus=self.corpus)],
            rag_retrieval_config=rag.RagRetrievalConfig(
                top_k=top_k, filter=rag.Filter(vector_distance_threshold=config.RAG_DISTANCE_THRESHOLD)
            ),
        )
        return [(c.source_uri or c.source_display_name, float(c.score)) for c in resp.contexts.contexts]  # pragma: no cover

    def search(self, query: str, top_k: int = 5, jurisdiction: str | None = None) -> list[Hit]:
        query = query.strip()
        if not query:
            return []
        try:
            raw = self._query(query, top_k * 2)  # over-fetch: the filters below may drop some
            self.last_error = None
        except Exception as exc:  # fail to refusal, never to parametric knowledge
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return []
        hits: list[Hit] = []
        for source, distance in raw:
            chunk = self._by_anchor.get(_anchor_from_source(source))
            if chunk is None or distance > config.RAG_DISTANCE_THRESHOLD:
                continue  # stale corpus file or insufficient relevance
            if not _in_jurisdiction(chunk, jurisdiction):
                continue
            tiebreak = self.PRIMARY_TIEBREAK if chunk.authority == "primary" else 0.0
            hits.append(Hit(chunk, max(0.0, 1.0 - distance) + tiebreak))
        return _rank(hits, top_k)


def _project_from_corpus(corpus: str) -> str | None:
    parts = corpus.split("/")
    return parts[1] if len(parts) > 1 and parts[0] == "projects" else None


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    if config.RAG_CORPUS:
        return VertexRagRetriever(config.RAG_CORPUS)
    return LocalBM25Retriever()  # tests / eval / offline


def spotlight(hits: list[Hit]) -> str:
    """Wrap retrieved text as untrusted reference data (SDD §4.3 spotlighting)."""
    blocks = []
    for h in hits:
        c = h.chunk
        blocks.append(
            f'<policy_excerpt citation="{c.citation_label}" anchor="{c.anchor}" '
            f'authority="{c.authority}" jurisdiction="{c.jurisdiction}">\n{c.text}\n</policy_excerpt>'
        )
    return "\n".join(blocks)


__all__ = ["Hit", "LocalBM25Retriever", "Retriever", "VertexRagRetriever", "chunk_object_name",
           "config", "get_retriever", "spotlight"]
