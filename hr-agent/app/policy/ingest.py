"""Handbook ingestion: parse → chunk → enrich → quality-gate (SDD §3.7).

Deterministic by design so that `corpus_version` (a content hash) pins exactly
which text any citation could have resolved to.

Corpus findings handled here:
  C-1  Misfiled content — §5.5 "Community Guidelines" contains the Relocation
       Allowance and ITSM lifecycle rules; §2.2 "Baby Bonding" contains the
       medical-leave email-delegation rule. These bullets are split into their own
       chunks and given a corrected `semantic_topic`.
  C-2  Summary/detail duplication — §1–§3 and §5.1–§5.3 are tagged
       `authority=summary`; the detailed sections are `primary`.
  C-3  Editorial artefacts — "Here is the drafted text…" lines are quarantined.
  C-4  Duplicate section numbers — anchors are keyed on slug + content hash.
  C-6  Jurisdiction — `SG` / `GLOBAL` / `ALL` on every chunk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from app import config

SECTION_RE = re.compile(r"^\*\*SECTION\s+(\d+):\s*(.+?)\*\*\s*$", re.IGNORECASE)
SUB_RE = re.compile(r"^\*\*(\d+)\.(\d+)\s+(.+?)\*\*\s*(.*)$")
META_COMMENTARY_RE = re.compile(
    r"(here is the drafted text|you can insert this into your|as the next section \(e\.g\.)",
    re.IGNORECASE,
)

# Sections whose content is restated in more detail elsewhere (C-2).
SUMMARY_SECTIONS = {"1", "2", "3"}
SUMMARY_SUBSECTIONS = {"5.1", "5.2", "5.3"}

# Bullet-level splits for misfiled content (C-1): (subsection, keyword) -> topic.
MISFILED_BULLETS: list[tuple[str, str, str]] = [
    ("5.5", "Relocation Allowance", "Relocation Allowance & Destination Building Access"),
    ("5.5", "IT Service Management", "ITSM Ticket Lifecycle & Priority Standards"),
    ("2.2", "Administrative Coverage", "Planned Medical Leave: Email Delegation to Manager"),
    ("5.4", "Home Office Equipment Allowance", "Home Office Equipment Allowance (Remote/Hybrid)"),
]


@dataclass
class Chunk:
    chunk_id: str
    section_number: str
    section_title: str
    subsection: str
    subsection_title: str
    semantic_topic: str
    jurisdiction: str
    authority: str
    content_hash: str
    anchor: str
    text: str
    line_start: int
    effective_date: str = "2026-07"
    misfiled_from: str | None = None

    @property
    def citation_label(self) -> str:
        return f"§{self.subsection or self.section_number} {self.semantic_topic}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["citation_label"] = self.citation_label
        return d


@dataclass
class IngestionReport:
    corpus_version: str
    chunks: list[Chunk]
    quarantined: list[dict] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def _clean(text: str) -> str:
    text = text.replace("\\&", "&").replace("\\$", "$").replace("\\>", ">").replace("\\-", "-")
    text = text.replace("&nbsp;", " ").replace("\\", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def _jurisdiction(*titles: str) -> str:
    joined = " ".join(titles).lower()
    if "(singapore)" in joined:
        return "SG"
    if "(global)" in joined or "global leaves" in joined:
        return "GLOBAL"
    return "ALL"


def _make_chunk(sec_no, sec_title, sub, sub_title, topic, text, line, misfiled_from=None) -> Chunk:
    text = _clean(text)
    h = hashlib.sha256(text.encode()).hexdigest()
    authority = (
        "summary" if sec_no in SUMMARY_SECTIONS or sub in SUMMARY_SUBSECTIONS else "primary"
    )
    anchor = f"s{sub or sec_no}-{_slug(topic)}-{h[:8]}"
    return Chunk(
        chunk_id=anchor,
        section_number=sec_no,
        section_title=_clean(sec_title),
        subsection=sub,
        subsection_title=_clean(sub_title),
        semantic_topic=_clean(topic),
        jurisdiction=_jurisdiction(sec_title, sub_title),
        authority=authority,
        content_hash=h,
        anchor=anchor,
        text=text,
        line_start=line,
        misfiled_from=misfiled_from,
    )


def _split_bullets(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Group raw lines into top-level bullets (continuations/sub-bullets attached)."""
    bullets: list[tuple[int, list[str]]] = []
    for ln, raw in lines:
        if raw.startswith("* ") or not bullets:
            bullets.append((ln, [raw]))
        else:
            bullets[-1][1].append(raw)
    return [(ln, "\n".join(parts)) for ln, parts in bullets]


def ingest(path: Path | None = None) -> IngestionReport:
    path = path or config.CORPUS_PATH
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    quarantined: list[dict] = []
    findings: list[str] = []
    # (sec_no, sec_title, sub, sub_title, first_line, [(line_no, text)])
    blocks: list[list] = []
    sec_no, sec_title = "0", "Preamble"
    seen_sections: dict[str, int] = {}

    for idx, line in enumerate(raw_lines, start=1):
        stripped = line.rstrip()
        if META_COMMENTARY_RE.search(stripped):
            quarantined.append({"line": idx, "text": stripped[:160], "reason": "C-3 meta-commentary"})
            continue
        if m := SECTION_RE.match(stripped):
            sec_no, sec_title = m.group(1), m.group(2)
            seen_sections[sec_no] = seen_sections.get(sec_no, 0) + 1
            if seen_sections[sec_no] > 1:
                findings.append(f"C-4 duplicate section number {sec_no} at line {idx}")
            blocks.append([sec_no, sec_title, "", sec_title, idx, []])
            continue
        if m := SUB_RE.match(stripped):
            sub = f"{m.group(1)}.{m.group(2)}"
            sub_title = m.group(3).rstrip(":")
            blocks.append([sec_no, sec_title, sub, sub_title, idx, []])
            if m.group(4).strip():
                blocks[-1][5].append((idx, m.group(4).strip()))
            continue
        if not stripped.strip() or stripped.strip() in {"---", "&nbsp;"}:
            continue
        if not blocks:
            blocks.append([sec_no, sec_title, "", sec_title, idx, []])
        blocks[-1][5].append((idx, stripped.strip()))

    chunks: list[Chunk] = []
    for s_no, s_title, sub, sub_title, first_line, lines in blocks:
        if not lines:
            continue
        splits = [(kw, topic) for (target, kw, topic) in MISFILED_BULLETS if target == sub]
        if splits:
            remaining: list[str] = []
            for ln, bullet in _split_bullets(lines):
                match = next(((kw, t) for kw, t in splits if kw.lower() in bullet.lower()), None)
                if match:
                    chunks.append(
                        _make_chunk(s_no, s_title, sub, sub_title, match[1], bullet, ln,
                                    misfiled_from=f"§{sub} {_clean(sub_title)}")
                    )
                    findings.append(f"C-1 re-titled bullet in §{sub} -> '{match[1]}' (line {ln})")
                else:
                    remaining.append(bullet)
            if remaining:
                chunks.append(
                    _make_chunk(s_no, s_title, sub, sub_title, sub_title, "\n".join(remaining), first_line)
                )
        else:
            text = "\n".join(t for _, t in lines)
            chunks.append(_make_chunk(s_no, s_title, sub, sub_title, sub_title, text, first_line))

    for raw in raw_lines:
        if re.search(r"\bWorkday\b", raw):
            findings.append("C-5 terminology drift: 'Workday' used for WorkWeek")
            break

    corpus_version = hashlib.sha256(
        "".join(c.content_hash for c in chunks).encode()
    ).hexdigest()[:12]
    return IngestionReport(corpus_version, chunks, quarantined, findings)


@lru_cache(maxsize=1)
def load_corpus() -> IngestionReport:
    return ingest()


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    rep = load_corpus()
    print(f"corpus_version={rep.corpus_version} chunks={len(rep.chunks)}")
    for q in rep.quarantined:
        print("QUARANTINED", q)
    for f in sorted(set(rep.findings)):
        print("FINDING", f)
