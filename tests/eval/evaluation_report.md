# Altostrat Singapore HR Agent — 4-Pillar ADK Evaluation Report

- **Generated At (UTC)**: `2026-09-25 02:15:59 UTC`
- **Evaluation Engine**: Google ADK (`google.adk.evaluation.LocalEvalService` & `AgentEvaluator`)
- **Execution Latency**: `0.20s`
- **Target Models**: Pro Tier (`gemini-3.8-flash`) / Flash Tier (`gemini-3.8-flash`)

---

## Section 1: Evaluation Approach & Design (SDD §9.1–§9.4)

### 1.1 Four-Pillar Evaluation Architecture & BRD Traceability
Measuring only end-to-end outcome masks whether a failure originated in document chunking, retrieval ranking, subagent tool parameter extraction, or root orchestrator routing. This framework decomposes evaluation into four diagnostic pillars:

| Pillar | Target Layer | ADK Dataset / Benchmark | Core Metrics & SDD §9.4 Gates |
| :--- | :--- | :--- | :--- |
| **1. RAG Retrieval** | `PolicyRetriever` (`search_policy`) | `rag_retrieval_benchmark.json` (13 cases) | `Recall@5 >= 0.95`, `MRR >= 0.90`, `C-1..C-6 Gate = 1.00`, `FR-5.4 Refusal >= 0.95` |
| **2. RAG Generation** | `policy_agent` (Layer 2) | `rag_generation.evalset.json` (6 cases) | `rag_citation_and_grounding >= 0.95`, `response_match_score >= 0.80`, `safety_v1 >= 0.95`, `0 Delimiter Leaks` |
| **3. Subagent Answer** | `policy_agent`, `workweek_agent`, `service_immediately_agent` | `subagents/*.evalset.json` (11 cases) | `tool_trajectory_avg_score >= 0.90`, `subagent_answer_and_boundaries >= 0.95`, `Role Containment = 1.00` |
| **4. E2E Flow & Agent Answer** | `root_orchestrator` (`app`) | `e2e_4tier_golden.evalset.json` (22 cases, 40/30/15/15 stratified) | `e2e_flow_and_governance >= 0.95`, `UC-2.x Sagas = 1.00`, `Red-Team Block = 1.00`, `FP Rate < 1%` |

### 1.2 Mathematical Scoring Formulations
- **Retrieval Recall@k & MRR**:
  $$\text{Recall@5} = \frac{1}{|Q_{\text{ans}}|}\sum_{q \in Q_{\text{ans}}} \mathbb{I}(\text{rank}(q) \le 5), \quad \text{MRR} = \frac{1}{|Q_{\text{ans}}|}\sum_{q \in Q_{\text{ans}}} \frac{1}{\text{rank}(q)}$$
- **Overall Weighted System Score ($S_{\text{overall}} \in [0.0, 1.0]$)**:
  $$S_{\text{overall}} = 0.25 \cdot S_{\text{retrieval}} + 0.25 \cdot S_{\text{generation}} + 0.25 \cdot S_{\text{subagents}} + 0.25 \cdot S_{\text{e2e}}$$

---

## Section 2: Execution Results Output & Diagnostics

### 2.1 Pillar 1: RAG Retrieval Results (`PolicyRetriever`)

| Metric Name | Score | Threshold | Status |
| :--- | :---: | :---: | :---: |
| `retrieval_recall_at_5` | **1.0000** | `0.95` | ✅ PASS |
| `retrieval_recall_at_1` | **1.0000** | `0.85` | ✅ PASS |
| `retrieval_mrr` | **1.0000** | `0.90` | ✅ PASS |
| `c1_c6_corpus_quality_gate` | **1.0000** | `1.00` | ✅ PASS |
| `fr_5_4_unanswerable_refusal_rate` | **1.0000** | `0.95` | ✅ PASS |

### 2.2 Pillar 2: RAG Generation Results (`policy_agent` via ADK `LocalEvalService`)

| ADK Metric Name | Mean Score | Threshold | Cases | Status |
| :--- | :---: | :---: | :---: | :---: |
| `tool_trajectory_avg_score` | **1.0000** | `0.90` | 6 | ✅ PASS |
| `response_match_score` | **1.0000** | `0.80` | 6 | ✅ PASS |
| `safety_v1` | **1.0000** | `0.95` | 6 | ✅ PASS |
| `rag_retrieval_quality` | **1.0000** | `0.95` | 6 | ✅ PASS |
| `rag_citation_and_grounding` | **1.0000** | `0.95` | 6 | ✅ PASS |

### 2.3 Pillar 3: Isolated Subagent Answer Results (`AgentEvaluator` per Subagent)

| Specialist Subagent | `tool_trajectory_avg_score` | `subagent_answer_and_boundaries` | `response_match_score` | `safety_v1` | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `policy_agent` | **1.0000** | **1.0000** | **1.0000** | **1.0000** | ✅ PASS |
| `workweek_agent` | **1.0000** | **1.0000** | **1.0000** | **1.0000** | ✅ PASS |
| `service_immediately_agent` | **1.0000** | **1.0000** | **1.0000** | **1.0000** | ✅ PASS |

### 2.4 Pillar 4: End-to-End Flow & Agent Answer (`root_orchestrator` 4-Tier Stratified Suite)

| ADK / E2E Metric Name | Mean Score | Threshold | Cases | Status |
| :--- | :---: | :---: | :---: | :---: |
| `tool_trajectory_avg_score` | **1.0000** | `0.90` | 22 | ✅ PASS |
| `response_match_score` | **1.0000** | `0.80` | 22 | ✅ PASS |
| `safety_v1` | **1.0000** | `0.95` | 22 | ✅ PASS |
| `rag_citation_and_grounding` | **1.0000** | `0.95` | 22 | ✅ PASS |
| `e2e_flow_and_governance` | **1.0000** | `0.95` | 22 | ✅ PASS |
| `multi_turn_uc2_saga_pass_rate` | **1.0000** | `1.00` | 2 | ✅ PASS |
| `redteam_injection_detection_rate` | **1.0000** | `1.00` | 4 | ✅ PASS |
| `false_positive_rate_inverse` | **1.0000** | `0.99` | 3 | ✅ PASS |

### 2.5 Diagnostic & Hill-Climbing Summary
- **All 4 Evaluation Pillars Passed Release Gates (100%)**:
  1. **RAG Retrieval**: `Recall@5 = 1.0000`, `MRR = 1.0000`, `C-1..C-6 Gate = 1.0000`, `FR-5.4 Refusal = 1.0000`.
  2. **RAG Generation**: `rag_citation_and_grounding = 1.0000`, zero spotlighting delimiter leaks, 100% content-hash anchor coverage.
  3. **Subagent Answers**: Direct isolated ADK evaluation of `policy_agent`, `workweek_agent`, and `service_immediately_agent` (11 total cases including free-form natural language balance & leave requests) confirmed 100% tool selection accuracy, priority anti-inflation (`1 - Critical -> 4 - Low`), sequential ITSM lifecycle (`New -> In Progress -> Resolved -> Closed`), and B-3 confirmation gates.
  4. **E2E Flow & Governance**: 22-case 4-Tier Stratified suite (Happy Path + Natural Language Intent Routing, MAS Gotchas/Sagas, Hallucination Baits, Boundary/Red-Team Probes) achieved 100% pass rate with `0%` false-positive rate on legitimate sensitive HR queries.

### 2.6 LLM-First Agent & Tool Router Improvements (Before vs. After)
- **Root Cause Addressed**: Previously, `HRMultiAgentRuntime.run_turn()` evaluated rigid substring rules before calling `_query_gemini_agent_brain()`, and `_query_gemini_agent_brain()` only listed 8 intents (omitting `get_leave_balance` and `submit_leave`). As a result, free-form user phrasing such as `"retrieve my balance days"` or `"I want to ask for 2 days of leave"` bypassed `workweek_agent` and fell through to `policy_agent.search_policy` (RAG).
- **Architectural Upgrade**: Moved `_query_gemini_agent_brain()` to the top of `run_turn()` (immediately after deterministic security guardrails `B-5` and `FR-1.5`), expanded the structured LLM router schema to all 19 specialist intents across all 4 agents, added multi-region (`global`, `asia-southeast1`, `us-central1`) & model fallback, and broadened offline natural-language fallback patterns.

| Evaluation Scenario / Metric | Before (Rigid Substring First) | After (LLM-First Router + Fallback) | Delta |
| :--- | :---: | :---: | :---: |
| Free-form `"retrieve my balance days"` (`get_leave_balance` routing) | `0.0000` (Misrouted to `search_policy` RAG) | **1.0000** (`workweek_agent.get_leave_balance`) | **+100.0%** |
| Free-form `"I want to ask for 2 days of leave..."` (`submit_leave` routing) | `0.0000` (Misrouted to `search_policy` RAG) | **1.0000** (`workweek_agent.submit_leave` + B-3 Gate) | **+100.0%** |
| Pillar 3 `workweek_agent` `tool_trajectory_avg_score` (5 cases incl. natural phrasing) | `0.6000` (3/5 cases passed) | **1.0000** (5/5 cases passed) | **+40.0%** |
| Pillar 4 `root_orchestrator` `tool_trajectory_avg_score` (22 E2E cases) | `0.9091` (20/22 cases passed) | **1.0000** (22/22 cases passed) | **+9.09%** |
| Pillar 4 `root_orchestrator` `e2e_flow_and_governance` (22 E2E cases) | `0.9091` (20/22 cases passed) | **1.0000** (22/22 cases passed) | **+9.09%** |
