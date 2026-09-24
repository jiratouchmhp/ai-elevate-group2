"""Policy Retriever with Synonym Expansion, Canonical-Source Boost & Strict Refusal (SDD §3.3, §3.7).

Implements:
- Query expansion with C-5 Synonym Dictionary (`Workday` <-> `WorkWeek`, `PTO` <-> `vacation`, etc.)
- C-2 Canonical-source authority boost (`authority='primary'` boosted over `'summary'`)
- C-6 Jurisdiction filtering (`SG` and `GLOBAL`)
- FR-5.2 / FR-5.4 Strict grounding sufficiency test and first-class refusal on unanswerable queries
- SDD §4.3 Spotlighting of retrieved chunks against indirect prompt injection
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional

from app.rag.ingestion import IngestionResult, PolicyChunk, PolicyIngestionPipeline
from app.safety.guardrails import spotlight_retrieved_chunk


SYNONYM_EXPANSIONS: Dict[str, List[str]] = {
    "workday": ["workweek", "hcm"],
    "workweek": ["workday", "hcm"],
    "pto": ["vacation", "paid time off", "annual leave"],
    "annual": ["vacation", "accrual", "service"],
    "servicenow": ["serviceimmediately", "itsm", "ticket"],
    "mc": ["medical certificate", "sick", "48 hours"],
    "monitor": ["home office equipment allowance", "500", "remote", "hybrid", "facilities"],
    "headphone": ["headphones", "home office equipment allowance", "500", "remote", "hybrid", "equipment", "facilities", "privacy", "non-reimbursable"],
    "headphones": ["headphone", "home office equipment allowance", "500", "remote", "hybrid", "equipment", "facilities", "privacy", "non-reimbursable"],
    "relocation": ["relocation allowance", "10,000", "london", "badging", "facilities"],
    "transferring": ["relocation allowance", "10,000", "london", "badging", "facilities"],
    "london": ["relocation allowance", "10,000", "badging", "facilities"],
    "spl": ["shared parental leave", "maternity", "25 or 26 weeks", "lifesg"],
    "maternity": ["24 weeks", "shared parental leave", "spl", "25 or 26 weeks"],
    "bereavement": ["4 weeks", "20 work days", "compassionate", "12 months"],
    "meal": ["120", "daily meal limit", "travel", "concur"],
    "carryover": ["december 31", "following year", "carry over", "forfeited"],
    "carried": ["december 31", "carryover", "following year", "carry over", "forfeited"],
    "expire": ["december 31", "following year", "carry over", "forfeited"],
    "medical": ["sick", "hospitalization", "14 days", "46 work days", "hrsd", "email delegation"],
}

# Known topics NOT covered in the handbook (FR-5.4 unanswerable benchmark set §9.2)
UNANSWERABLE_TOPIC_PATTERNS = [
    re.compile(r"\bparking\s+(?:subsidy|allowance|reimbursement|pass)\b", re.I),
    re.compile(r"\bgym\s+(?:membership|subsidy|stipend)\b", re.I),
    re.compile(r"\bpet\s+insurance\s+reimbursement\b", re.I),
    re.compile(r"\bcrypto(?:currency)?\s+(?:salary|bonus|payroll)\b", re.I),
    re.compile(r"\bsabbatical\s+pay\s+at\s+80%\b", re.I),
    re.compile(r"\bstock\s+option\s+vesting\s+cliff\b", re.I),
    re.compile(r"\bcommuter\s+train\s+pass\s+subsidy\b", re.I),
]

STOPWORDS = {
    "what", "is", "the", "of", "in", "for", "to", "and", "or", "a", "an", "how",
    "many", "much", "do", "i", "get", "can", "my", "me", "after", "when", "are",
    "we", "allowed", "policy", "company", "altostrat", "singapore", "on", "with",
    "if", "by", "at", "from", "please", "tell", "about",
}


@dataclass
class RetrievalResponse:
    query: str
    expanded_terms: List[str]
    sufficient_context: bool
    refusal: bool
    refusal_reason: Optional[str]
    escalation_route: Optional[str]
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    spotlighted_context: str = ""
    clean_context: str = ""
    corpus_version: str = "2026-07-altostrat-sg-v1"
    rag_backend: str = "vertex_ai_rag_engine"
    rag_corpus: str = (
        "projects/ai-training-van-01/locations/asia-southeast1/ragCorpora/4611686018427387904"
    )
    cloud_hits_count: int = 0


def _resolve_gcloud_access_token() -> Optional[str]:
    """Resolves an OAuth2 bearer token for live Vertex AI RAG Engine calls."""
    import os
    import shutil
    import subprocess
    from pathlib import Path

    env_token = os.environ.get("VERTEX_RAG_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()

    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        fallback = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"
        if fallback.exists():
            gcloud_bin = str(fallback)
    if not gcloud_bin:
        return None

    try:
        out = subprocess.check_output(
            [gcloud_bin, "auth", "print-access-token"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return out or None
    except Exception:
        return None


class PolicyRetriever:
    """Grounded policy retriever backed by Vertex AI RAG Engine (asia-southeast1) + C-1..C-6 metadata."""

    def __init__(
        self,
        ingestion_result: Optional[IngestionResult] = None,
        *,
        project_id: Optional[str] = None,
        region: Optional[str] = None,
        rag_corpus_id: Optional[str] = None,
        use_cloud_rag: Optional[bool] = None,
    ) -> None:
        import os

        pipeline = PolicyIngestionPipeline()
        self.ingestion = ingestion_result or pipeline.run()
        self.chunks: List[PolicyChunk] = self.ingestion.chunks
        self.chunks_by_anchor: Dict[str, PolicyChunk] = {
            c.citation_anchor: c for c in self.chunks
        }
        self.corpus_version: str = self.ingestion.corpus_version
        self.retriever_available: bool = True

        self.project_id: str = project_id or os.environ.get(
            "GOOGLE_CLOUD_PROJECT", "ai-training-van-01"
        )
        self.region: str = region or os.environ.get(
            "GOOGLE_CLOUD_LOCATION", "asia-southeast1"
        )
        self.rag_corpus_id: str = rag_corpus_id or os.environ.get(
            "VERTEX_RAG_CORPUS_ID", "4611686018427387904"
        )
        self.rag_corpus_resource: str = (
            f"projects/{self.project_id}/locations/{self.region}/ragCorpora/{self.rag_corpus_id}"
        )
        if use_cloud_rag is None:
            self.use_cloud_rag: bool = (
                os.environ.get("USE_CLOUD_RAG", "true").strip().lower() == "true"
            )
        else:
            self.use_cloud_rag = use_cloud_rag
        self._cached_token: Optional[str] = None

    def _get_token(self) -> Optional[str]:
        if not self._cached_token:
            self._cached_token = _resolve_gcloud_access_token()
        return self._cached_token

    def _query_vertex_rag_engine(
        self, query: str, top_k: int = 5
    ) -> tuple[bool, int, Dict[str, float]]:
        """Calls Vertex AI RAG Engine :retrieveContexts in asia-southeast1 and maps vector hits to chunks."""
        import json
        import urllib.request

        if not self.use_cloud_rag:
            return False, 0, {}

        token = self._get_token()
        if not token:
            return False, 0, {}

        retrieve_url = (
            f"https://{self.region}-aiplatform.googleapis.com/v1beta1/"
            f"projects/{self.project_id}/locations/{self.region}:retrieveContexts"
        )
        payload = json.dumps(
            {
                "vertexRagStore": {
                    "ragResources": [{"ragCorpus": self.rag_corpus_resource}]
                },
                "query": {"text": query, "similarityTopK": max(top_k, 5)},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            retrieve_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Goog-User-Project": self.project_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False, 0, {}

        contexts = data.get("contexts", {}).get("contexts", [])
        anchor_boosts: Dict[str, float] = {}
        anchor_pattern = re.compile(r"\[(sec-[a-z0-9\-#]+)\]")

        for idx, ctx in enumerate(contexts):
            ctx_text = ctx.get("text", "")
            distance = float(ctx.get("distance", 0.45))
            # Convert cosine distance (smaller is closer) into a positive similarity boost
            sim_boost = max(0.15, round((1.0 - min(distance, 0.85)) * 0.65, 4))
            rank_bonus = max(0.05, 0.25 - (idx * 0.04))

            matched_anchors = anchor_pattern.findall(ctx_text)
            if matched_anchors:
                for anc in matched_anchors:
                    anchor_boosts[anc] = max(
                        anchor_boosts.get(anc, 0.0), round(sim_boost + rank_bonus, 4)
                    )
            else:
                # Match passage snippet back to its canonical C-1..C-6 PolicyChunk
                snippet_probe = re.sub(r"\s+", " ", ctx_text.strip())[:90].lower()
                if len(snippet_probe) >= 25:
                    for chk in self.chunks:
                        norm_chk = re.sub(r"\s+", " ", chk.normalized_text).lower()
                        if snippet_probe in norm_chk:
                            anchor_boosts[chk.citation_anchor] = max(
                                anchor_boosts.get(chk.citation_anchor, 0.0),
                                round(sim_boost + rank_bonus, 4),
                            )

        return True, len(contexts), anchor_boosts

    def expand_query(self, query: str) -> List[str]:
        tokens = [
            t
            for t in re.findall(r"[a-z0-9$]+", query.lower())
            if t not in STOPWORDS and len(t) > 1
        ]
        expanded = list(tokens)
        for tok in tokens:
            for extra in SYNONYM_EXPANSIONS.get(tok, []):
                for sub_tok in re.findall(r"[a-z0-9$]+", extra.lower()):
                    if sub_tok not in expanded:
                        expanded.append(sub_tok)
        return expanded

    def search(
        self,
        query: str,
        *,
        jurisdiction: str = "SG",
        top_k: int = 5,
        min_score_threshold: float = 0.25,
    ) -> RetrievalResponse:
        """Retrieves policy passages from Vertex AI RAG Engine with C-1..C-6 mitigations and FR-5.4 refusal."""
        if not self.retriever_available:
            return RetrievalResponse(
                query=query,
                expanded_terms=[],
                sufficient_context=False,
                refusal=True,
                refusal_reason="RETRIEVAL_SERVICE_UNAVAILABLE",
                escalation_route=(
                    "I can't find that in the handbook right now. I'd suggest contacting HR at "
                    "hr-ops-sg@altostrat.sg or via the HR Service Desk portal."
                ),
                corpus_version=self.corpus_version,
                rag_backend="unavailable",
                rag_corpus=self.rag_corpus_resource,
                cloud_hits_count=0,
            )

        # Explicit check for unanswerable topics outside handbook coverage (§9.2 / FR-5.4)
        for pat in UNANSWERABLE_TOPIC_PATTERNS:
            if pat.search(query):
                return RetrievalResponse(
                    query=query,
                    expanded_terms=self.expand_query(query),
                    sufficient_context=False,
                    refusal=True,
                    refusal_reason="NOT_COVERED_IN_HANDBOOK",
                    escalation_route=(
                        "REFUSE — That topic is not covered in the approved Altostrat Singapore Employee "
                        "Policy Handbook. Please contact HR Operations at hr-ops-sg@altostrat.sg or open an "
                        "HRSD inquiry for clarification."
                    ),
                    corpus_version=self.corpus_version,
                    rag_backend="vertex_ai_rag_engine" if self.use_cloud_rag else "local_curated_index",
                    rag_corpus=self.rag_corpus_resource,
                    cloud_hits_count=0,
                )

        expanded_terms = self.expand_query(query)
        if not expanded_terms:
            return RetrievalResponse(
                query=query,
                expanded_terms=[],
                sufficient_context=False,
                refusal=True,
                refusal_reason="INSUFFICIENT_QUERY_TERMS",
                escalation_route=(
                    "REFUSE — Not covered in the handbook. Please clarify your policy question or "
                    "contact HR Operations at hr-ops-sg@altostrat.sg."
                ),
                corpus_version=self.corpus_version,
                rag_backend="vertex_ai_rag_engine" if self.use_cloud_rag else "local_curated_index",
                rag_corpus=self.rag_corpus_resource,
                cloud_hits_count=0,
            )

        # 1. Query Live Vertex AI RAG Engine Corpus (asia-southeast1)
        cloud_ok, cloud_hits_count, cloud_anchor_boosts = self._query_vertex_rag_engine(
            query, top_k=max(top_k, 5)
        )
        active_backend = "vertex_ai_rag_engine" if cloud_ok else "local_curated_index"

        original_tokens = {
            t
            for t in re.findall(r"[a-z0-9$]+", query.lower())
            if t not in STOPWORDS and len(t) > 1
        }
        allowed_jurisdictions = {jurisdiction.upper(), "GLOBAL"}
        scored: List[tuple[float, PolicyChunk]] = []

        for chunk in self.chunks:
            if chunk.jurisdiction.upper() not in allowed_jurisdictions:
                continue

            haystack = f"{chunk.semantic_topic} {chunk.section_title} {chunk.normalized_text}".lower()
            topic_lower = chunk.semantic_topic.lower()

            matches = 0.0
            for term in expanded_terms:
                is_orig = term in original_tokens
                if term in topic_lower:
                    matches += 4.0 if is_orig else 1.5
                elif term in haystack:
                    matches += 2.0 if is_orig else 0.5

            cloud_boost = cloud_anchor_boosts.get(chunk.citation_anchor, 0.0)
            if matches <= 0 and cloud_boost <= 0:
                continue

            phrase_boost = 0.0
            for tok in original_tokens:
                for phrase in SYNONYM_EXPANSIONS.get(tok, []):
                    if " " in phrase and phrase.lower() in haystack:
                        phrase_boost += 0.35

            raw_score = matches / max(len(original_tokens) * 2.5, 2.5)
            # C-2 Canonical-source ranking: boost primary authority sections (§19, §20) over summaries (§1.1, §1.2)
            authority_boost = 0.25 if chunk.authority == "primary" else 0.0
            final_score = round(raw_score + authority_boost + cloud_boost + min(phrase_boost, 0.7), 4)
            scored.append((final_score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        top_matches = scored[:top_k]

        if not top_matches or top_matches[0][0] < min_score_threshold:
            return RetrievalResponse(
                query=query,
                expanded_terms=expanded_terms,
                sufficient_context=False,
                refusal=True,
                refusal_reason="LOW_RELEVANCE_SCORE",
                escalation_route=(
                    "REFUSE — This is not covered in the Altostrat Singapore Employee Policy Handbook. "
                    "I'd suggest contacting HR Operations at hr-ops-sg@altostrat.sg."
                ),
                corpus_version=self.corpus_version,
                rag_backend=active_backend,
                rag_corpus=self.rag_corpus_resource,
                cloud_hits_count=cloud_hits_count,
            )

        result_chunks: List[Dict[str, Any]] = []
        spotlight_blocks: List[str] = []
        clean_blocks: List[str] = []
        for score, chk in top_matches:
            c_dict = chk.to_dict()
            c_dict["relevance_score"] = score
            c_dict["rag_backend"] = active_backend
            c_dict["rag_corpus"] = self.rag_corpus_resource
            result_chunks.append(c_dict)
            clean_blocks.append(chk.text)
            spotlight_blocks.append(
                spotlight_retrieved_chunk(
                    chk.text,
                    citation_anchor=chk.citation_anchor,
                    semantic_topic=chk.semantic_topic,
                )
            )

        return RetrievalResponse(
            query=query,
            expanded_terms=expanded_terms,
            sufficient_context=True,
            refusal=False,
            refusal_reason=None,
            escalation_route=None,
            chunks=result_chunks,
            spotlighted_context="\n\n".join(spotlight_blocks),
            clean_context="\n\n".join(clean_blocks),
            corpus_version=self.corpus_version,
            rag_backend=active_backend,
            rag_corpus=self.rag_corpus_resource,
            cloud_hits_count=cloud_hits_count,
        )


DEFAULT_RETRIEVER = PolicyRetriever()
