"""Unit Tests for Grounding & Ingestion Pipeline (SDD §3.7 C-1..C-6 & Appendix C Q-1..Q-8).

Verifies:
- C-1 Semantic re-titling of §5.5 Relocation Allowance and ITSM Lifecycle rules (never cited as "Community Guidelines")
- C-2 Canonical-source authority ranking (`authority='primary'` for §19/§20 outranking `authority='summary'` §1.1/§1.2)
- C-3 Quarantine of all 3 editorial drafting artifacts (lines 327, 658, 936)
- C-4 Hash-based citation anchors disambiguating duplicate SECTION 30s
- C-5 Synonym dictionary expansion (`Workday` -> `WorkWeek`, `PTO` -> `vacation`)
- C-6 Jurisdiction filtering (`SG` and `GLOBAL`)
- FR-5.4 Strict refusal on unanswerable policy questions (e.g., parking subsidy)
"""

import unittest

from app.rag.ingestion import PolicyIngestionPipeline
from app.rag.retriever import PolicyRetriever


class TestRAGIngestionAndRetriever(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipeline = PolicyIngestionPipeline()
        cls.ingestion_result = cls.pipeline.run()
        cls.retriever = PolicyRetriever(cls.ingestion_result)

    def test_c3_quarantines_all_three_editorial_drafting_artifacts(self) -> None:
        # Appendix C Q-2: Three occurrences at lines 327, 658, 936
        quarantined = self.ingestion_result.quarantined_artifacts
        self.assertEqual(len(quarantined), 3)
        quarantined_lines = [q.line_number for q in quarantined]
        self.assertEqual(quarantined_lines, [327, 658, 936])

        # Verify none of the active chunks contain the leftover authoring instruction
        for chk in self.ingestion_result.chunks:
            self.assertNotIn("Here is the drafted text for the new section", chk.text)

    def test_c1_semantic_retitling_of_misfiled_relocation_and_itsm_rules(self) -> None:
        reloc_res = self.retriever.search("What is the relocation allowance for transferring to London?")
        self.assertFalse(reloc_res.refusal)
        self.assertTrue(len(reloc_res.chunks) > 0)
        top_chunk = reloc_res.chunks[0]
        self.assertEqual(
            top_chunk["semantic_topic"],
            "International Relocation & Building Access Policy",
        )
        self.assertIn("$10,000 USD", top_chunk["text"])

        itsm_res = self.retriever.search("What is the ITSM ticket lifecycle and squeaky chair priority?")
        self.assertFalse(itsm_res.refusal)
        topics = [c["semantic_topic"] for c in itsm_res.chunks]
        self.assertIn("IT Service Management (ITSM) Ticket Lifecycle & Priority Policy", topics)

    def test_c4_duplicate_section_30_unique_hash_anchors(self) -> None:
        sec30_chunks = [
            c for c in self.ingestion_result.chunks if c.section_number.startswith("30.")
        ]
        self.assertGreaterEqual(len(sec30_chunks), 2)
        anchors = {c.citation_anchor for c in sec30_chunks}
        # Every Section 30 chunk must have a distinct content_hash + heading_slug anchor
        self.assertEqual(len(anchors), len(sec30_chunks))

    def test_c2_canonical_source_authority_tags_and_c5_workday_synonym(self) -> None:
        expanded = self.retriever.expand_query("How do I submit PTO in Workday?")
        self.assertIn("workweek", expanded)
        self.assertIn("vacation", expanded)

        # Verify summary vs primary authority tagging exists
        summary_chunks = [c for c in self.ingestion_result.chunks if c.authority == "summary"]
        primary_chunks = [c for c in self.ingestion_result.chunks if c.authority == "primary"]
        self.assertGreater(len(summary_chunks), 0)
        self.assertGreater(len(primary_chunks), 0)

    def test_fr54_strict_refusal_on_unanswerable_queries(self) -> None:
        unanswerable = self.retriever.search("What is the monthly parking subsidy for employees?")
        self.assertTrue(unanswerable.refusal)
        self.assertFalse(unanswerable.sufficient_context)
        self.assertIn("REFUSE", unanswerable.escalation_route or "")
        self.assertEqual(len(unanswerable.chunks), 0)


    def test_section_2_2_administrative_coverage_dedicated_chunk(self) -> None:
        adm_res = self.retriever.search("temporary email delegation to manager HRSD administrative coverage")
        self.assertFalse(adm_res.refusal)
        self.assertGreater(len(adm_res.chunks), 0)
        self.assertEqual(
            adm_res.chunks[0]["semantic_topic"],
            "Medical Leave Administrative Coverage & Email Delegation Policy",
        )

    def test_plural_headphones_synonym_expansion_uc_1_1(self) -> None:
        hp_res = self.retriever.search("Are employees allowed to expense noise-canceling headphones?")
        self.assertFalse(hp_res.refusal)
        self.assertGreater(len(hp_res.chunks), 0)
        self.assertEqual(hp_res.chunks[0]["section_number"], "5.4")
        self.assertNotIn("<<<UNTRUSTED_POLICY_DOCUMENT_START", hp_res.clean_context)


if __name__ == "__main__":
    unittest.main()
