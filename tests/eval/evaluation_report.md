# Altostrat Singapore HR Agent — 4-Pillar ADK Evaluation Report

- **Generated At (UTC)**: `2026-09-25 01:30:42 UTC`
- **Evaluation Engine**: Google ADK (`google.adk.evaluation.LocalEvalService` & `AgentEvaluator`)
- **Execution Latency**: `4.98s`
- **Target Models**: Pro Tier (`gemini-3.8-flash`) / Flash Tier (`gemini-3.8-flash`)

---

## Section 1: Evaluation Approach & Design (SDD §9.1–§9.4)

### 1.1 Four-Pillar Evaluation Architecture & BRD Traceability
Measuring only end-to-end outcome masks whether a failure originated in document chunking, retrieval ranking, subagent tool parameter extraction, or root orchestrator routing. This framework decomposes evaluation into four diagnostic pillars:

| Pillar | Target Layer | ADK Dataset / Benchmark | Core Metrics & SDD §9.4 Gates |
| :--- | :--- | :--- | :--- |
| **1. RAG Retrieval** | `PolicyRetriever` (`search_policy`) | `rag_retrieval_benchmark.json` (13 cases) | `Recall@5 >= 0.95`, `MRR >= 0.90`, `C-1..C-6 Gate = 1.00`, `FR-5.4 Refusal >= 0.95` |
| **2. RAG Generation** | `policy_agent` (Layer 2) | `rag_generation.evalset.json` (6 cases) | `rag_citation_and_grounding >= 0.95`, `response_match_score >= 0.80`, `safety_v1 >= 0.95`, `0 Delimiter Leaks` |
| **3. Subagent Answer** | `policy_agent`, `workweek_agent`, `service_immediately_agent` | `subagents/*.evalset.json` (9 cases) | `tool_trajectory_avg_score >= 0.90`, `subagent_answer_and_boundaries >= 0.95`, `Role Containment = 1.00` |
| **4. E2E Flow & Agent Answer** | `root_orchestrator` (`app`) | `e2e_4tier_golden.evalset.json` (20 cases, 40/30/15/15 stratified) | `e2e_flow_and_governance >= 0.95`, `UC-2.x Sagas = 1.00`, `Red-Team Block = 1.00`, `FP Rate < 1%` |

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
| `workweek_agent` | **1.0000** | **1.0000** | **0.9697** | **1.0000** | ✅ PASS |
| `service_immediately_agent` | **1.0000** | **1.0000** | **1.0000** | **1.0000** | ✅ PASS |

### 2.4 Pillar 4: End-to-End Flow & Agent Answer (`root_orchestrator` 4-Tier Stratified Suite)

| ADK / E2E Metric Name | Mean Score | Threshold | Cases | Status |
| :--- | :---: | :---: | :---: | :---: |
| `tool_trajectory_avg_score` | **1.0000** | `0.90` | 20 | ✅ PASS |
| `response_match_score` | **1.0000** | `0.80` | 20 | ✅ PASS |
| `safety_v1` | **1.0000** | `0.95` | 20 | ✅ PASS |
| `rag_citation_and_grounding` | **1.0000** | `0.95` | 20 | ✅ PASS |
| `e2e_flow_and_governance` | **1.0000** | `0.95` | 20 | ✅ PASS |
| `multi_turn_uc2_saga_pass_rate` | **1.0000** | `1.00` | 2 | ✅ PASS |
| `redteam_injection_detection_rate` | **1.0000** | `1.00` | 4 | ✅ PASS |
| `false_positive_rate_inverse` | **1.0000** | `0.99` | 3 | ✅ PASS |

### 2.5 Diagnostic & Hill-Climbing Summary
- **All 4 Evaluation Pillars Passed Release Gates (100%)**:
  1. **RAG Retrieval**: `Recall@5 = 1.0000`, `MRR = 1.0000`, `C-1..C-6 Gate = 1.0000`, `FR-5.4 Refusal = 1.0000`.
  2. **RAG Generation**: `rag_citation_and_grounding = 1.0000`, zero spotlighting delimiter leaks, 100% content-hash anchor coverage.
  3. **Subagent Answers**: Direct isolated ADK evaluation of `policy_agent`, `workweek_agent`, and `service_immediately_agent` confirmed 100% tool selection accuracy, priority anti-inflation (`1 - Critical -> 4 - Low`), sequential ITSM lifecycle (`New -> In Progress -> Resolved -> Closed`), and B-3 confirmation gates.
  4. **E2E Flow & Governance**: 20-case 4-Tier Stratified suite (40% Happy Path, 30% MAS Gotchas/Sagas, 15% Hallucination Baits, 15% Boundary/Red-Team Probes) achieved 100% pass rate with `0%` false-positive rate on legitimate sensitive HR queries.
