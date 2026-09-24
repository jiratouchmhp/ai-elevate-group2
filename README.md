# Altostrat Singapore — HR Agentic Assistant (MVP 1)

Implementation of the **MVP 1 Solution Design Document (`docs/sdd.md`)** and **Business Requirements Document (`docs/brd.md`)** built on **Google ADK 2+ (`google-adk`)**.

---

## Architecture & Five-Plane Topology (`docs/sdd.md` §1.3)

| Plane | Module(s) | Responsibility |
| :--- | :--- | :--- |
| **1 · Experience Plane** | [`app/ui/ag_ui_server.py`](app/ui/ag_ui_server.py), [`app/ui/frontend/App.tsx`](app/ui/frontend/App.tsx) | React + Vite TypeScript components (`CitationChip`, `ConfirmationCard`) & Cloud Run BFF serving **AG-UI over SSE** behind IAP (`x-goog-authenticated-user-email` -> `employee_id`). |
| **2 · Safety & Trust Plane** | [`app/safety/guardrails.py`](app/safety/guardrails.py) | **Model Armor** input/output scanning (`INSPECT_ONLY` shadow mode & `INSPECT_AND_BLOCK` enforce mode, fail-closed §5.4), **Advanced SDP** with custom Singapore NRIC/FIN detector (`CON-7`), **Spotlighting** against indirect injection (§4.3), and **Cheap-Path FAQ Cache** (§3.2). |
| **3 · Agent Plane (ADK 2+)** | [`app/agent.py`](app/agent.py), [`app/callbacks/adk_callbacks.py`](app/callbacks/adk_callbacks.py), [`app/tools/agent_tools.py`](app/tools/agent_tools.py) | Hierarchical multi-agent system (**D2**) with per-agent model tiering (**D6**): `root_agent` (`gemini-2.5-pro`, zero tools — delegation only), `policy_agent` (`gemini-2.5-pro`, read-only `search_policy`), `workweek_agent` (`gemini-2.5-flash`, 7 HCM tools), and `service_immediately_agent` (`gemini-2.5-flash`, 5 ITSM tools). |
| **4 · Integration Plane** | [`app/pdp/rules_engine.py`](app/pdp/rules_engine.py), [`app/config/rules.yaml`](app/config/rules.yaml), [`app/acl/mcp_proxy.py`](app/acl/mcp_proxy.py), [`app/ledger/transaction_ledger.py`](app/ledger/transaction_ledger.py) | **Policy Decision Point (PDP)** enforcing all 9 deterministic business rules (`LEAVE_BALANCE_CAP`, `LEAVE_CHRONOLOGY`, `LEAVE_NOTICE_15D`, `TICKET_LIFECYCLE`, `TICKET_PRIORITY_FLOOR`, `TICKET_DEDUPE`, `EQUIP_ELIGIBILITY`, `RELOCATION_CAP`, `CONTACT_FORMAT`) + `B-3` confirmation & `B-8` resolution gates; **MCP-to-MCP Interception Proxy (D9)** with SPIFFE **Agent Identity (D10)** and per-persona `X-MCP-Token` PATs (**OQ-12**); **Transaction Ledger** with idempotency & Saga partial-completion compensation (§3.6). |
| **5 · Grounding & Data Plane** | [`app/rag/ingestion.py`](app/rag/ingestion.py), [`app/rag/retriever.py`](app/rag/retriever.py) | Layout-aware handbook ingestion & retrieval implementing all 6 corpus mitigations (`C-1` semantic re-titling of §5.5 Relocation/ITSM rules, `C-2` canonical authority ranking, `C-3` quarantine of lines 327/658/936 drafting artifacts, `C-4` duplicate Section 30 hash anchors, `C-5` Workday/WorkWeek synonym expansion, `C-6` SG/GLOBAL jurisdiction filtering). |
| **6 · Governance Plane** | [`app/governance/audit_logger.py`](app/governance/audit_logger.py), [`deployment/terraform/single-project/main.tf`](deployment/terraform/single-project/main.tf) | 100% structured audit logging (including denials) with `correlation_id`, `actor_type="AUTOMATED_AGENT"`, `agent_version`, `prompt_version`, `rules_version`, `corpus_version`, and Terraform IaC (`asia-southeast1` + PSC endpoint for Model Armor). |

---

## Running Tests & Evaluation Gate

```bash
python3 -m unittest discover -s tests -v
```
