"""Corpus Ingestion Pipeline with Layout-Aware Chunking & Quality Gate (SDD §3.7, Appendix C).

Mitigates all 6 corpus defects documented in SDD §3.7:
- C-1: Semantic re-titling for misfiled Relocation Allowance and ITSM Lifecycle rules in §5.5.
- C-2: Canonical-source authority tagging (`authority='primary'` for §19/§20 vs `'summary'` for §1.1/§1.2).
- C-3: Ingestion quality gate quarantining leftover editorial instructions at lines 327, 658, 936.
- C-4: Content-hash + heading-slug anchors (`sec-30-...#<hash>`) disambiguating duplicate SECTION 30s.
- C-5: Terminology drift detection (`Workday` -> `WorkWeek` normalized in index metadata).
- C-6: Jurisdiction tagging (`SG` vs `GLOBAL`) on every chunk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional


DEFAULT_HANDBOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "ALTOSTRAT SINGAPORE EMPLOYEE POLICY HANDBOOK & CONDUCT GUIDELINES.md"
)


@dataclass
class PolicyChunk:
    chunk_id: str
    section_number: str
    section_title: str
    parent_section_title: str
    semantic_topic: str
    jurisdiction: str  # "SG" or "GLOBAL"
    authority: str  # "primary" or "summary"
    effective_date: str
    content_hash: str
    citation_anchor: str
    deep_link_url: str
    text: str
    normalized_text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuarantinedArtifact:
    line_number: int
    reason: str
    raw_text: str


@dataclass
class IngestionResult:
    corpus_version: str
    chunks: List[PolicyChunk]
    quarantined_artifacts: List[QuarantinedArtifact]
    terminology_drift_occurrences: List[Dict[str, Any]] = field(default_factory=list)


class PolicyIngestionPipeline:
    """Layout-aware parser and quality-gate filter for the Altostrat Singapore Handbook."""

    # C-3 Quality Gate: Rejects leftover LLM drafting instructions (Appendix C Q-2 / Q-8)
    EDITORIAL_META_PATTERN = re.compile(
        r"Here is the drafted text for the new section.*?You can insert this into your Altostrat Singapore handbook",
        re.IGNORECASE | re.DOTALL,
    )

    MAIN_SECTION_PATTERN = re.compile(r"^\*\*SECTION\s+(\d+)\s*:\s*(.+?)\*\*\s*$", re.IGNORECASE)
    SUB_SECTION_PATTERN = re.compile(r"^\*\*(\d+\.\d+)\s+(.+?)\*\*(.*)$")

    def __init__(
        self,
        handbook_path: Optional[Path] = None,
        corpus_version: str = "2026-07-altostrat-sg-v1",
    ) -> None:
        self.handbook_path = handbook_path or DEFAULT_HANDBOOK_PATH
        self.corpus_version = corpus_version

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:64] or "section"

    @staticmethod
    def _derive_jurisdiction(title: str, text: str) -> str:
        combined = f"{title} {text[:200]}"
        if "(Singapore)" in combined or "Singapore" in title or "MSF" in combined or "LifeSG" in combined:
            return "SG"
        if "(Global)" in combined:
            return "GLOBAL"
        return "SG"

    @staticmethod
    def _derive_authority(section_number: str) -> str:
        """C-2 Canonical-source ranking: §19 and §20 are detailed primary sources; §1.1/§1.2 are summaries."""
        if section_number.startswith(("1.1", "1.2")):
            return "summary"
        if section_number.startswith(("19", "20")):
            return "primary"
        return "primary"

    @staticmethod
    def _derive_semantic_topic(section_number: str, section_title: str, text: str) -> str:
        """C-1 Semantic re-titling at ingestion.

        Specifically rescues Relocation Allowance and ITSM Lifecycle / Priority rules
        misfiled under §5.5 'Community Guidelines (Conversational Boundaries)'.
        """
        lower_text = text.lower()
        if section_number == "5.5":
            if "relocation allowance" in lower_text or "building badging" in lower_text:
                return "International Relocation & Building Access Policy"
            if "ticket lifecycle" in lower_text or "priority definitions" in lower_text:
                return "IT Service Management (ITSM) Ticket Lifecycle & Priority Policy"
        if section_number == "2.2" and "administrative coverage" in lower_text:
            return "Medical Leave Administrative Coverage & Email Delegation Policy"
        if section_number == "5.4" and "home office equipment allowance" in lower_text:
            return "Remote & Hybrid Work — Home Office Equipment Allowance ($500 USD)"
        clean_title = re.sub(r"\s*\((?:Singapore|Global)\)\s*", "", section_title, flags=re.I).strip()
        return clean_title

    def run(self) -> IngestionResult:
        if not self.handbook_path.exists():
            return IngestionResult(
                corpus_version=self.corpus_version,
                chunks=[],
                quarantined_artifacts=[],
            )

        raw_lines = self.handbook_path.read_text(encoding="utf-8").splitlines()
        quarantined: List[QuarantinedArtifact] = []
        drift_records: List[Dict[str, Any]] = []
        clean_lines: List[tuple[int, str]] = []

        for idx, line in enumerate(raw_lines, start=1):
            # C-3 Quality gate check
            if self.EDITORIAL_META_PATTERN.search(line):
                quarantined.append(
                    QuarantinedArtifact(
                        line_number=idx,
                        reason="C-3_EDITORIAL_DRAFTING_ARTIFACT_QUARANTINED",
                        raw_text=line.strip(),
                    )
                )
                continue

            # C-5 Track Workday -> WorkWeek terminology drift (Appendix C Q-4)
            if re.search(r"\bWorkday\b", line, re.IGNORECASE):
                drift_records.append({"line_number": idx, "raw_line": line.strip()})

            clean_lines.append((idx, line))

        chunks: List[PolicyChunk] = []
        current_parent_num = "0"
        current_parent_title = "General Policy"
        current_sub_num = "0.0"
        current_sub_title = "Overview"
        buffer: List[str] = []

        def flush_section(sec_num: str, sec_title: str, parent_title: str, body_lines: List[str]) -> None:
            body_text = "\n".join(body_lines).strip()
            if not body_text or len(body_text) < 20:
                return

            # Special C-1 splitting for §5.5 so Relocation Allowance and ITSM Lifecycle
            # each get their own dedicated, semantically re-titled chunk!
            if sec_num == "2.2":
                sub_segments_22: List[tuple[str, str]] = []
                bbl_lines: List[str] = []
                for bl in body_lines:
                    if "Administrative Coverage:" in bl:
                        sub_segments_22.append(
                            ("Medical Leave Administrative Coverage & Email Delegation Policy", bl)
                        )
                    else:
                        bbl_lines.append(bl)
                if bbl_lines:
                    sub_segments_22.insert(
                        0,
                        ("Baby Bonding Leave", "\n".join(bbl_lines)),
                    )
                for seg_idx, (sem_topic, seg_text) in enumerate(sub_segments_22):
                    self._append_chunk(
                        chunks=chunks,
                        sec_num=sec_num,
                        sec_title=sec_title,
                        parent_title=parent_title,
                        body_text=seg_text,
                        override_semantic_topic=sem_topic,
                        sub_index=seg_idx,
                    )
                return

            if sec_num == "5.5":
                sub_segments: List[tuple[str, str]] = []
                community_lines: List[str] = []
                for bl in body_lines:
                    if "Relocation Allowance:" in bl:
                        sub_segments.append(
                            ("International Relocation & Building Access Policy", bl)
                        )
                    elif "IT Service Management (ITSM) Guidelines" in bl or "Ticket Lifecycle:" in bl:
                        sub_segments.append(
                            ("IT Service Management (ITSM) Ticket Lifecycle & Priority Policy", bl)
                        )
                    else:
                        community_lines.append(bl)
                if community_lines:
                    sub_segments.insert(
                        0,
                        ("Community Guidelines & Conversational Boundaries", "\n".join(community_lines)),
                    )
                for seg_idx, (sem_topic, seg_text) in enumerate(sub_segments):
                    self._append_chunk(
                        chunks=chunks,
                        sec_num=sec_num,
                        sec_title=sec_title,
                        parent_title=parent_title,
                        body_text=seg_text,
                        override_semantic_topic=sem_topic,
                        sub_index=seg_idx,
                    )
                return

            self._append_chunk(
                chunks=chunks,
                sec_num=sec_num,
                sec_title=sec_title,
                parent_title=parent_title,
                body_text=body_text,
            )

        for _, line in clean_lines:
            stripped = line.strip()
            main_match = self.MAIN_SECTION_PATTERN.match(stripped)
            if main_match:
                flush_section(current_sub_num, current_sub_title, current_parent_title, buffer)
                buffer = []
                current_parent_num = main_match.group(1).strip()
                current_parent_title = main_match.group(2).strip()
                current_sub_num = f"{current_parent_num}.0"
                current_sub_title = current_parent_title
                continue

            sub_match = self.SUB_SECTION_PATTERN.match(stripped)
            if sub_match:
                flush_section(current_sub_num, current_sub_title, current_parent_title, buffer)
                current_sub_num = sub_match.group(1).strip()
                current_sub_title = sub_match.group(2).strip()
                trailing = sub_match.group(3).strip()
                buffer = [trailing] if trailing else []
                continue

            buffer.append(line)

        flush_section(current_sub_num, current_sub_title, current_parent_title, buffer)

        return IngestionResult(
            corpus_version=self.corpus_version,
            chunks=chunks,
            quarantined_artifacts=quarantined,
            terminology_drift_occurrences=drift_records,
        )

    def _append_chunk(
        self,
        *,
        chunks: List[PolicyChunk],
        sec_num: str,
        sec_title: str,
        parent_title: str,
        body_text: str,
        override_semantic_topic: Optional[str] = None,
        sub_index: int = 0,
    ) -> None:
        contextual_text = (
            f"[Parent Section: {parent_title} | Subsection {sec_num}: {sec_title}]\n{body_text.strip()}"
        )
        content_hash = hashlib.sha256(contextual_text.encode("utf-8")).hexdigest()[:12]
        semantic_topic = override_semantic_topic or self._derive_semantic_topic(
            sec_num, sec_title, body_text
        )
        slug = self._slugify(f"{sec_num}-{semantic_topic}")
        # C-4: Citation anchor keyed on content_hash + heading_slug (never section number alone)
        citation_anchor = f"sec-{slug}#{content_hash[:8]}"
        deep_link_url = f"https://policies.altostrat.sg/handbook/{self.corpus_version}/{citation_anchor}"
        jurisdiction = self._derive_jurisdiction(sec_title, body_text)
        authority = self._derive_authority(sec_num)

        # C-5 Normalize 'Workday' -> 'WorkWeek' in normalized_text for retrieval
        normalized_text = re.sub(r"\bWorkday\b", "WorkWeek (Workday)", contextual_text, flags=re.I)

        chunk_id = f"chk-{sec_num.replace('.', '_')}-{sub_index}-{content_hash[:6]}"
        chunks.append(
            PolicyChunk(
                chunk_id=chunk_id,
                section_number=sec_num,
                section_title=sec_title,
                parent_section_title=parent_title,
                semantic_topic=semantic_topic,
                jurisdiction=jurisdiction,
                authority=authority,
                effective_date="2026-07-01",
                content_hash=content_hash,
                citation_anchor=citation_anchor,
                deep_link_url=deep_link_url,
                text=contextual_text,
                normalized_text=normalized_text,
            )
        )
