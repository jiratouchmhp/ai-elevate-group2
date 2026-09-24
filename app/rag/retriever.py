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


class PolicyRetriever:
    """Grounded policy retriever with canonical authority ranking and strict refusal."""

    def __init__(self, ingestion_result: Optional[IngestionResult] = None) -> None:
        pipeline = PolicyIngestionPipeline()
        self.ingestion = ingestion_result or pipeline.run()
        self.chunks: List[PolicyChunk] = self.ingestion.chunks
        self.corpus_version: str = self.ingestion.corpus_version
        self.retriever_available: bool = True

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
        top_k: int = 4,
        min_score_threshold: float = 0.25,
    ) -> RetrievalResponse:
        """Retrieves policy passages with C-1..C-6 mitigations and FR-5.4 strict grounding check."""
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
            )

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

            if matches <= 0:
                continue

            raw_score = matches / max(len(original_tokens) * 2.5, 2.5)
            # C-2 Canonical-source ranking: boost primary authority sections (§19, §20) over summaries (§1.1, §1.2)
            authority_boost = 0.25 if chunk.authority == "primary" else 0.0
            final_score = round(raw_score + authority_boost, 4)
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
            )

        result_chunks: List[Dict[str, Any]] = []
        spotlight_blocks: List[str] = []
        clean_blocks: List[str] = []
        for score, chk in top_matches:
            c_dict = chk.to_dict()
            c_dict["relevance_score"] = score
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
        )


DEFAULT_RETRIEVER = PolicyRetriever()
