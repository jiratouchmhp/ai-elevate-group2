"""End-to-End Integration Tests for ADK Agent -> Vertex AI RAG Engine Retrieval (BRD UC-1.1, UC-2.1..2.3).

Verifies that `HRMultiAgentRuntime` -> `root_orchestrator` -> `policy_agent` -> `search_policy`
actively retrieves HR policy passages from the Vertex AI RAG Engine Corpus
(`projects/ai-training-van-01/locations/asia-southeast1/ragCorpora/4611686018427387904`)
across single-domain and cross-system BRD workflows while enforcing C-1..C-6 and FR-5.4 guardrails.
"""

from __future__ import annotations

import unittest

from app.acl.mcp_proxy import AntiCorruptionLayerProxy, MockVendorBackendState
from app.agent import HRMultiAgentRuntime
from app.governance.audit_logger import AuditLogger
from app.ledger.transaction_ledger import TransactionLedger
from app.pdp.rules_engine import PolicyDecisionPoint
from app.rag.retriever import PolicyRetriever, _resolve_gcloud_access_token


EXPECTED_CORPUS = (
    "projects/ai-training-van-01/locations/asia-southeast1/ragCorpora/4611686018427387904"
)


class TestAgentVertexRagEndToEnd(unittest.TestCase):
    """End-to-end verification that the ADK agent retrieves HR policy from Vertex AI RAG Engine."""

    def setUp(self) -> None:
        self.backend = MockVendorBackendState()
        self.ledger = TransactionLedger()
        self.audit = AuditLogger()
        self.pdp = PolicyDecisionPoint(ledger=self.ledger)
        self.acl = AntiCorruptionLayerProxy(
            pdp=self.pdp,
            ledger=self.ledger,
            audit_logger=self.audit,
            backend_state=self.backend,
        )
        self.retriever = PolicyRetriever(
            project_id="ai-training-van-01",
            region="asia-southeast1",
            rag_corpus_id="4611686018427387904",
            use_cloud_rag=True,
        )
        self.runtime = HRMultiAgentRuntime(
            acl_proxy=self.acl,
            retriever=self.retriever,
            ledger=self.ledger,
            audit_logger=self.audit,
        )
        self.has_cloud_auth = _resolve_gcloud_access_token() is not None

    def _assert_vertex_rag_used(self, turn_result) -> None:
        self.assertIn("policy_agent", turn_result.delegated_agents)
        self.assertIn("search_policy", turn_result.tool_trajectory)
        self.assertGreater(len(turn_result.citations), 0)

        top_citation = turn_result.citations[0]
        self.assertEqual(top_citation.get("rag_corpus"), EXPECTED_CORPUS)
        if self.has_cloud_auth:
            self.assertEqual(top_citation.get("rag_backend"), "vertex_ai_rag_engine")

        # Verify AuditLogger recorded the search_policy call against Vertex AI RAG Engine
        policy_audits = [
            entry for entry in self.audit._records if entry.tool_invoked == "search_policy"
        ]
        self.assertGreater(len(policy_audits), 0)
        latest_audit = policy_audits[-1]
        self.assertEqual(
            latest_audit.tool_args_redacted.get("rag_corpus"),
            EXPECTED_CORPUS,
        )
        if self.has_cloud_auth:
            self.assertEqual(
                latest_audit.tool_args_redacted.get("rag_backend"),
                "vertex_ai_rag_engine",
            )
            self.assertIn("vertex_ai_rag_engine", latest_audit.notes)

    def test_uc_1_1_policy_qa_retrieves_from_vertex_rag_engine(self) -> None:
        """BRD UC-1.1: Single-domain Policy Q&A retrieves from Vertex AI RAG Engine with citations."""
        turn = self.runtime.run_turn(
            "What is the company's bereavement leave policy?",
            session_id="sess-e2e-uc11",
            authenticated_employee_id="EMP-SG-001",
        )
        self.assertFalse(turn.refusal)
        self._assert_vertex_rag_used(turn)

        # Direct check on retriever cloud_hits_count when authenticated
        direct = self.retriever.search("What is the company's bereavement leave policy?")
        if self.has_cloud_auth:
            self.assertEqual(direct.rag_backend, "vertex_ai_rag_engine")
            self.assertGreater(direct.cloud_hits_count, 0)

    def test_uc_2_1_equipment_procurement_retrieves_policy_from_vertex_rag(self) -> None:
        """BRD UC-2.1: Cross-system Equipment Procurement queries Vertex AI RAG Engine first."""
        turn = self.runtime.run_turn(
            "I saw I'm eligible for a home office monitor under the remote work policy. "
            "Can you verify my remote status and order one for me?",
            session_id="sess-e2e-uc21",
            authenticated_employee_id="EMP-SG-001",
            confirmed=False,
        )
        self.assertFalse(turn.refusal)
        self._assert_vertex_rag_used(turn)
        self.assertIn("workweek_agent", turn.delegated_agents)

    def test_uc_2_2_medical_leave_retrieves_policy_from_vertex_rag(self) -> None:
        """BRD UC-2.2: Cross-system Medical Leave queries Vertex AI RAG Engine before WorkWeek/ITSM."""
        turn = self.runtime.run_turn(
            "I need to take short-term medical leave starting next Monday. "
            "What is the process, and can you set it up for me?",
            session_id="sess-e2e-uc22",
            authenticated_employee_id="EMP-SG-001",
            confirmed=False,
        )
        self.assertFalse(turn.refusal)
        self._assert_vertex_rag_used(turn)

    def test_uc_2_3_relocation_retrieves_10k_cap_from_vertex_rag(self) -> None:
        """BRD UC-2.3: Cross-system London Relocation retrieves §5.5 $10,000 cap from Vertex AI RAG Engine."""
        turn = self.runtime.run_turn(
            "I'm transferring to the London office next month. Can you tell me the relocation "
            "allowance, update my record, and get my building access sorted?",
            session_id="sess-e2e-uc23",
            authenticated_employee_id="EMP-SG-001",
            confirmed=False,
        )
        self.assertFalse(turn.refusal)
        self._assert_vertex_rag_used(turn)
        topics = [c["semantic_topic"] for c in turn.citations]
        self.assertIn("International Relocation & Building Access Policy", topics)
        self.assertIn("$10,000", turn.response_text)

    def test_fr_5_4_strict_refusal_on_unanswerable_policy_query(self) -> None:
        """BRD FR-5.4: Unanswerable policy question triggers deterministic refusal and escalation."""
        turn = self.runtime.run_turn(
            "What is the company cryptocurrency salary bonus policy?",
            session_id="sess-e2e-refusal",
            authenticated_employee_id="EMP-SG-001",
        )
        self.assertTrue(turn.refusal)
        self.assertIn("REFUSE", turn.response_text)
        self.assertIn("hr-ops-sg@altostrat.sg", turn.response_text)


if __name__ == "__main__":
    unittest.main()
