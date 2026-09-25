"""Input/output safety screening (SDD D5 layer 1 + 4, FR-1.3, T-1/T-7).

Two interchangeable classifiers behind one interface:
  * `LocalHeuristicClassifier` — deterministic patterns for direct injection,
    jailbreak, bulk data exfiltration and violent/unsafe requests. Tuned against the
    false-positive probe set ("kill the process", "harassment policy", "terminate").
  * `ModelArmorClassifier` — Model Armor templates (enabled when
    MODEL_ARMOR_TEMPLATE_ID is set). Fails CLOSED on any error (§5.4).

Off-topic handling is deliberately left to the orchestrator's instructions (domain
containment, FR-5.4) rather than a keyword blocker, to protect the <1% FP target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config


@dataclass
class Verdict:
    blocked: bool
    categories: list[str] = field(default_factory=list)
    source: str = "local"
    confidence: str = "HIGH"

    def to_dict(self) -> dict:
        return {"blocked": self.blocked, "categories": self.categories, "source": self.source,
                "confidence": self.confidence}


_PATTERNS: dict[str, list[str]] = {
    "prompt_injection": [
        # Eval RCA v2 (rt-fp-016): "the"/"any" as the middle word made "ignore the old carryover rules from
        # the previous handbook" a false positive. Agent-directed qualifiers only; bare "the <rules>" must be adjacent.
        r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,40}\b(previous|prior|above|earlier|all|your|system|safety)\b.{0,30}\b(instruction|instructions|rules?|prompt|guardrails?|polic(y|ies)|restrictions?|validation|checks?|constraints?)\b",
        r"\b(ignore|disregard|forget|override|bypass)\s+(the|any|these|those)\s+(instructions?|rules|prompt|guardrails?|restrictions?|validation|checks?|constraints?)\b",
        r"\bskip\b.{0,25}\bconfirmation\b",
        r"\b(repeat|print|output|copy)\b.{0,25}\b(all|everything|the (text|content|message)s?)\b.{0,30}\babove\b",
        r"\b(mint|create|generate|issue|give me)\b.{0,25}\b(mcp|api|access|admin)\s*(token|key)s?\b",
        # Multilingual "ignore previous instructions" (zh / th / ms) — Model Armor is the primary control.
        r"(忽略|无视|忽視|無視).{0,10}(指令|指示|提示)",
        r"(เพิกเฉย|ละเว้น|ไม่ต้องสนใจ).{0,20}คำสั่ง",
        r"\b(abaikan|lupakan)\b.{0,30}\b(arahan|perintah)\b",
        r"\b(reveal|show|print|repeat|output|leak|tell me)\b.{0,30}\b(your|the)\b.{0,20}\b(system prompt|instructions|hidden prompt|initial prompt|developer message|configuration|secret|api key|token)s?\b",
        r"\bnew (system )?instructions?\s*[:：]",
        r"</?\s*(system|instructions?|admin|developer)\s*>",
        r"\[\s*(system|admin|developer)\s*(override|mode|message)?\s*\]",
        r"#{2,}\s*(system|instruction|override)",
        r"\b(you are|you're) (now|no longer)\b.{0,40}\b(unrestricted|admin|root|developer|dan|jailbroken|free|without (rules|restrictions))\b",
        r"\bact as\b.{0,20}\b(an? )?(system administrator|sysadmin|hr admin|administrator|root user|superuser|developer mode)\b",
        r"\b(developer|god|admin|debug|maintenance|sudo) mode\b",
        r"\b(jailbreak|jailbroken|do anything now|\bDAN\b)",
        r"\bpretend (that )?(you|there) (are|is|have) no (rules|restrictions|guardrails|limits)\b",
        r"\bwithout (any )?(the )?(validation|pdp|policy checks?|guardrails?|confirmation)\b",
        r"\b(set|use|pass|change)\b.{0,20}\bemployee[_ ]?id\b.{0,15}(?:\bto\b|=|\bas\b)\s*\"?EMP\d+",  # eval RCA v2: "\b=\b" could never match
        r"\bi am (the|an?) (hr )?(admin|administrator|system|developer)\b.{0,40}\b(authori[sz]e|allow|override|grant)",
    ],
    "data_exfiltration": [
        r"\b(all|every|entire|whole)\b.{0,20}\b(employees?'?|staff|workers?|users?)\b.{0,25}\b(records?|data|salar(y|ies)|balances?|addresses|phone numbers|nric|profiles?|details)\b",
        r"\b(dump|export|download|list)\b.{0,20}\b(the )?(database|all records|employee (table|directory|database))\b",
        r"\bselect \* from\b",
    ],
    "unsafe_violent": [
        r"\b(kill|murder|hurt|stab|shoot|poison|attack)\b.{0,15}\b(him|her|them|my (manager|boss|colleague|coworker|co-worker|team ?mate)|someone|people|a person)\b",
        r"\b(make|build|create)\b.{0,15}\b(bomb|explosive|weapon|poison)\b",
    ],
    "self_harm": [
        r"\b(suicide|suicidal|kill myself|end my life|hurt myself|self[- ]harm)\b",
        # eval RCA v2 (rt-tx-005): inflected forms were missed.
        r"\b(end(ing)?|take|taking) (my|my own) life\b|\bend(ing)? it all\b|\bwant(ed)? to die\b|\bdon'?t want to (live|be alive)\b",
    ],
    "hate_harassment_generation": [
        r"\b(write|draft|generate|compose)\b.{0,40}\b(insult|slur|racist|sexist|threatening|harass(ing|ment)? (message|email|note))\b",
        r"\b(write|draft|generate)\b.{0,30}\b(humiliat|demean|degrad)\w*\b.{0,30}\b(message|email|post|note)\b",
        r"\b(write|draft|generate|compose)\b.{0,20}\b(message|email|post|note)\b.{0,40}\b(humiliat|demean|degrad|belittl|mock)\w*",
    ],
}
_COMPILED = {k: [re.compile(p, re.IGNORECASE | re.DOTALL) for p in v] for k, v in _PATTERNS.items()}
_B64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LEET_WORD = re.compile(r"\b(?=\w*[a-z])(?=\w*[013457])\w{3,}\b", re.IGNORECASE)


def _deleet(text: str) -> str | None:
    """Leetspeak-normalised copy (eval RCA v2, rt-di-007), only when ≥3 mixed letter/digit words."""
    if len(_LEET_WORD.findall(text or "")) < 3:
        return None
    return _LEET_WORD.sub(lambda m: m.group(0).translate(_LEET), text)


class LocalHeuristicClassifier:
    source = "local_heuristics"

    def classify(self, text: str, direction: str = "input") -> Verdict:
        variants = [text or ""]
        if direction == "input" and (d := _deleet(text or "")):
            variants.append(d)
        cats = [cat for cat, pats in _COMPILED.items() if any(p.search(v) for p in pats for v in variants)]
        if direction == "input" and _B64_BLOB.search(text or ""):
            cats.append("obfuscation_encoded_payload")
        return Verdict(bool(cats), cats, self.source)


class ModelArmorClassifier:  # pragma: no cover - requires a GCP Model Armor template
    source = "model_armor"

    def __init__(self) -> None:
        import os

        from google.api_core.client_options import ClientOptions
        from google.cloud import modelarmor_v1  # type: ignore

        self._m = modelarmor_v1
        loc = config.MODEL_ARMOR_LOCATION
        base = f"projects/{os.environ.get('GOOGLE_CLOUD_PROJECT')}/locations/{loc}/templates/"
        self._names = {"input": base + config.MODEL_ARMOR_TEMPLATE_ID_INPUT,
                       "output": base + config.MODEL_ARMOR_TEMPLATE_ID_OUTPUT}
        self._client = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(api_endpoint=f"modelarmor.{loc}.rep.googleapis.com"))

    def classify(self, text: str, direction: str = "input") -> Verdict:
        try:
            item = self._m.DataItem(text=text)
            if direction == "input":
                resp = self._client.sanitize_user_prompt(
                    request=self._m.SanitizeUserPromptRequest(name=self._names["input"], user_prompt_data=item))
            else:
                resp = self._client.sanitize_model_response(
                    request=self._m.SanitizeModelResponseRequest(name=self._names["output"],
                                                                model_response_data=item))
            result = resp.sanitization_result
            match = result.filter_match_state == self._m.FilterMatchState.MATCH_FOUND
            cats = []
            if match:
                for name, fr in result.filter_results.items():
                    # Each FilterResult is a oneof; report the filters that matched.
                    for field_name in ("pi_and_jailbreak_filter_result", "malicious_uri_filter_result",
                                       "rai_filter_result", "sdp_filter_result", "csam_filter_filter_result"):
                        sub = getattr(fr, field_name, None)
                        state = getattr(sub, "match_state", None) if sub is not None else None
                        if state is None and sub is not None:  # sdp result nests inspect/deidentify
                            inner = getattr(sub, "inspect_result", None) or getattr(sub, "deidentify_result", None)
                            state = getattr(inner, "match_state", None)
                        if state == self._m.FilterMatchState.MATCH_FOUND:
                            cats.append(name)
                            break
            return Verdict(match, cats or (["model_armor_match"] if match else []), self.source)
        except Exception as exc:  # fail closed (§5.4)
            return Verdict(True, [f"model_armor_unavailable:{type(exc).__name__}"], self.source, "FAIL_CLOSED")


class CompositeClassifier:
    """Model Armor (when configured) AND local heuristics — defence in depth (D5)."""

    def __init__(self) -> None:
        self.local = LocalHeuristicClassifier()
        enabled = config.MODEL_ARMOR_TEMPLATE_ID_INPUT or config.MODEL_ARMOR_TEMPLATE_ID_OUTPUT
        self.remote = ModelArmorClassifier() if enabled else None

    def classify(self, text: str, direction: str = "input") -> Verdict:
        v = self.local.classify(text, direction)
        if self.remote is not None:
            r = self.remote.classify(text, direction)
            if r.blocked:
                return Verdict(True, v.categories + r.categories, f"{v.source}+{r.source}", r.confidence)
        return v
