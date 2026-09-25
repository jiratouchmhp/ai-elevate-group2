"""SPII detection + de-identification (SDD §4.5, FR-1.4, CON-7).

Model Armor's *basic* SDP has no Singapore NRIC/FIN detector, so the design
mandates a custom infoType. This module is the local equivalent: a checksum-
validated NRIC/FIN detector plus common SPII patterns, used before persistence
(audit/logs) and on inbound/outbound text.
"""

from __future__ import annotations

import re

NRIC_RE = re.compile(r"\b([STFGM])(\d{7})([A-Z])\b", re.IGNORECASE)
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
SG_PHONE_RE = re.compile(r"(?<!\w)(?:\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?!\w)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

_ST = "JZIHGFEDCBA"
_FG = "XWUTRQPNMLK"
_M = "KLJNPQRTUWX"
_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)


def is_valid_nric(value: str) -> bool:
    m = NRIC_RE.fullmatch(value.strip())
    if not m:
        return False
    prefix, digits, check = m.group(1).upper(), m.group(2), m.group(3).upper()
    total = sum(int(d) * w for d, w in zip(digits, _WEIGHTS, strict=False))
    if prefix in "TG":
        total += 4
    elif prefix == "M":
        total += 3
    table = _ST if prefix in "ST" else _FG if prefix in "FG" else _M
    return table[total % 11] == check


def find_nric(text: str, require_checksum: bool = False) -> list[str]:
    found = [m.group(0) for m in NRIC_RE.finditer(text or "")]
    return [f for f in found if is_valid_nric(f)] if require_checksum else found


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str, *, phones: bool = True, emails: bool = False) -> tuple[str, list[str]]:
    """Return (redacted_text, infotypes_found). NRIC/FIN is always redacted."""
    if not text:
        return text, []
    found: list[str] = []

    def _nric(m: re.Match) -> str:
        found.append("SG_NRIC_FIN")
        return "[REDACTED_NRIC]"

    out = NRIC_RE.sub(_nric, text)

    def _card(m: re.Match) -> str:
        if luhn_ok(m.group(0)):
            found.append("CREDIT_CARD")
            return "[REDACTED_CARD]"
        return m.group(0)

    out = CARD_RE.sub(_card, out)
    if phones:
        out, n = SG_PHONE_RE.subn("[REDACTED_PHONE]", out)
        found += ["PHONE_NUMBER"] * n
    if emails:
        out, n = EMAIL_RE.subn("[REDACTED_EMAIL]", out)
        found += ["EMAIL_ADDRESS"] * n
    return out, sorted(set(found))


def redact_obj(obj, **kw):
    """Recursively redact strings inside dict/list payloads (for audit args)."""
    if isinstance(obj, str):
        return redact(obj, **kw)[0]
    if isinstance(obj, dict):
        return {k: ("[REDACTED_ADDRESS]" if k in ("address", "ship_to") and isinstance(v, str) else redact_obj(v, **kw))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, **kw) for v in obj]
    return obj
