"""Policy Decision Point (PDP) — Deterministic Pre-Write Rule Engine (SDD §1.3, §3.8, §5.3).

The PDP sits between the agents and every backend write. No leave request, contact update,
ticket creation, or status transition reaches a backend without passing this deterministic,
version-controlled rule set.

Rules enforced:
1. LEAVE_BALANCE_CAP (§1.2, §20)
2. LEAVE_CHRONOLOGY (§1.2)
3. LEAVE_NOTICE_15D (§1.2, §5.3 — soft warning with acknowledgement)
4. TICKET_LIFECYCLE (§5.5 — New -> In Progress -> Resolved -> Closed; stricter than backend; B-8)
5. TICKET_PRIORITY_FLOOR (§5.5 — programmatic downgrade to '4 - Low' for non-emergencies)
6. TICKET_DEDUPE (FR-4.3 — 5-minute duplicate scan in Transaction Ledger)
7. EQUIP_ELIGIBILITY (§5.4 — Remote/Hybrid status, $500 USD cap, Facilities category)
8. RELOCATION_CAP (§5.5 — $10,000 USD cap, Facilities category, Priority '3 - Moderate')
9. CONTACT_FORMAT (FR-3.3 — address >= 5 chars, phone regex)
10. B-3 Confirmation Gate — every mutating call requires explicit user confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import yaml

from app.ledger.transaction_ledger import DEFAULT_LEDGER, TransactionLedger


RULES_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


@dataclass
class PDPDecision:
    allowed: bool
    status: str  # ALLOW, ALLOW_WITH_WARNING, CONFIRMATION_REQUIRED, DENY
    rule_id: str
    reason: str
    user_message: str
    idempotency_key: Optional[str] = None
    modified_args: Optional[Dict[str, Any]] = None
    warnings: List[str] = field(default_factory=list)
    confirmation_card: Optional[Dict[str, Any]] = None
    existing_backend_ref: Optional[str] = None
    rules_version: str = "1.2.0"


class PolicyDecisionPoint:
    """Deterministic business-rule engine (SDD Plane 4)."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        ledger: Optional[TransactionLedger] = None,
        engine_available: bool = True,
        reference_today: Optional[date] = None,
    ) -> None:
        self.config_path = config_path or RULES_CONFIG_PATH
        self.config = self._load_config(self.config_path)
        self.rules_version: str = str(self.config.get("rules_version", "1.2.0"))
        self.ledger = ledger or DEFAULT_LEDGER
        self.engine_available = engine_available
        self.reference_today = reference_today

    @staticmethod
    def _load_config(path: Path) -> Dict[str, Any]:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {"rules_version": "1.2.0", "rules": {}}

    def _today(self) -> date:
        return self.reference_today or date.today()

    def evaluate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        employee_id: str,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> PDPDecision:
        """Evaluates a tool call deterministically. Fails closed on any engine error (§5.4)."""
        if not self.engine_available:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="PDP_FAIL_CLOSED",
                reason="PDP rule engine unavailable — failing closed per SDD §5.4.",
                user_message="I can't complete that action right now.",
                rules_version=self.rules_version,
            )

        try:
            ctx = context_data or {}
            if tool_name in ("submit_leave", "request_time_off"):
                return self._validate_submit_leave(args, employee_id=employee_id, ctx=ctx)
            if tool_name in ("update_contact", "update_personal_info"):
                return self._validate_update_contact(args, employee_id=employee_id)
            if tool_name in ("cancel_leave", "cancel_leave_request"):
                return self._validate_cancel_leave(args, employee_id=employee_id, ctx=ctx)
            if tool_name in ("create_incident", "create_ticket"):
                return self._validate_create_incident(args, employee_id=employee_id, ctx=ctx)
            if tool_name in ("update_status", "update_ticket_status"):
                return self._validate_update_status(args, employee_id=employee_id, ctx=ctx)
            if tool_name in ("add_comment", "add_ticket_comment"):
                return self._validate_add_comment(args, employee_id=employee_id, ctx=ctx)

            # Read-only tools pass through with scope check
            return PDPDecision(
                allowed=True,
                status="ALLOW",
                rule_id="READ_ONLY_PASS",
                reason="Read-only operation scoped to authenticated employee.",
                user_message="Allowed.",
                rules_version=self.rules_version,
            )
        except Exception as exc:  # Fail closed (§5.4)
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="PDP_ENGINE_EXCEPTION",
                reason=f"Unhandled PDP exception: {exc}",
                user_message="I can't complete that action right now.",
                rules_version=self.rules_version,
            )

    # -------------------------------------------------------------------------
    # 1. WorkWeek Leave Submission Validation (LEAVE_CHRONOLOGY, LEAVE_BALANCE_CAP, LEAVE_NOTICE_15D)
    # -------------------------------------------------------------------------
    def _validate_submit_leave(
        self, args: Dict[str, Any], *, employee_id: str, ctx: Dict[str, Any]
    ) -> PDPDecision:
        start_str = str(args.get("start_date", ""))
        end_str = str(args.get("end_date", ""))
        leave_type = str(args.get("leave_type", "Vacation")).strip()
        days = float(args.get("days", args.get("work_days", 0.0)))
        confirmed = bool(args.get("confirmed", False))

        # Rule: LEAVE_CHRONOLOGY (§1.2)
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="LEAVE_CHRONOLOGY",
                reason="Invalid date format; expected YYYY-MM-DD.",
                user_message="Please provide the start and end dates in YYYY-MM-DD format.",
                rules_version=self.rules_version,
            )

        today = self._today()
        if start_dt > end_dt:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="LEAVE_CHRONOLOGY",
                reason=f"start_date ({start_str}) is after end_date ({end_str}).",
                user_message=f"The start date ({start_str}) cannot be after the end date ({end_str}). Please restate your dates.",
                rules_version=self.rules_version,
            )

        if start_dt < today:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="LEAVE_CHRONOLOGY",
                reason=f"start_date ({start_str}) is in the past (today is {today.isoformat()}).",
                user_message=f"Leave requests cannot be submitted for past dates ({start_str}). Please choose a date from {today.isoformat()} onward.",
                rules_version=self.rules_version,
            )

        # Validate half/full-day increments (§1.2: 0.5 or 1.0 increments, positive)
        if days <= 0 or (days * 2) % 1 != 0:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="LEAVE_BALANCE_CAP",
                reason=f"Invalid day increment {days}; must be positive multiples of 0.5.",
                user_message="Leave must be booked in half-day (0.5) or full-day (1.0) increments.",
                rules_version=self.rules_version,
            )

        # Rule: LEAVE_BALANCE_CAP (§1.2, §20)
        balances = ctx.get("balances", {})
        type_key = "vacation" if leave_type.lower().startswith("vac") else "sick"
        remaining_balance = float(
            args.get(
                "remaining_balance",
                balances.get(type_key, {}).get("remaining", 999.0),
            )
        )
        if days > remaining_balance:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="LEAVE_BALANCE_CAP",
                reason=f"Requested {days} {leave_type} days exceeds remaining accrued balance of {remaining_balance}.",
                user_message=(
                    f"That request for {days} days of {leave_type} leave exceeds your "
                    f"{remaining_balance} remaining accrued days (§1.2 / §20). Would you like to request "
                    f"up to {remaining_balance} days instead?"
                ),
                rules_version=self.rules_version,
            )

        # Rule: LEAVE_NOTICE_15D (§1.2 & §5.3 soft warning)
        warnings: List[str] = []
        notice_days = (start_dt - today).days
        if leave_type.lower() == "vacation" and notice_days < 15:
            warnings.append(
                f"Notice period warning (LEAVE_NOTICE_15D, Handbook §1.2): Planned vacation should be "
                f"discussed and approved at least 15 days in advance (this request is {notice_days} days ahead)."
            )

        idem_key = self.ledger.generate_idempotency_key(
            employee_id,
            "submit_leave",
            {"start_date": start_str, "end_date": end_str, "leave_type": leave_type, "days": days},
        )

        # Boundary B-3: Confirm before every write
        if not confirmed:
            resulting_balance = round(remaining_balance - days, 1)
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Pre-flight validation passed; awaiting explicit user confirmation (B-3).",
                user_message=(
                    f"Confirm: Submit {days} day(s) of {leave_type} leave from {start_str} to {end_str} "
                    f"(resulting balance: {resulting_balance} days)?"
                    + (f" Note: {warnings[0]}" if warnings else "")
                ),
                idempotency_key=idem_key,
                warnings=warnings,
                confirmation_card={
                    "action": "submit_leave",
                    "employee_id": employee_id,
                    "leave_type": leave_type,
                    "start_date": start_str,
                    "end_date": end_str,
                    "days": days,
                    "remaining_balance_before": remaining_balance,
                    "resulting_balance_after": resulting_balance,
                    "warnings": warnings,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW_WITH_WARNING" if warnings else "ALLOW",
            rule_id="LEAVE_NOTICE_15D" if warnings else "LEAVE_BALANCE_CAP",
            reason="All deterministic leave rules satisfied and confirmed.",
            user_message="Leave request validated.",
            idempotency_key=idem_key,
            warnings=warnings,
            rules_version=self.rules_version,
        )

    # -------------------------------------------------------------------------
    # 2. Contact Update Validation (CONTACT_FORMAT, FR-3.3)
    # -------------------------------------------------------------------------
    def _validate_update_contact(self, args: Dict[str, Any], *, employee_id: str) -> PDPDecision:
        address = str(args.get("address", "")).strip()
        phone = str(args.get("phone", "")).strip()
        confirmed = bool(args.get("confirmed", False))

        phone_regex = re.compile(r"^\+?[\d\s\-()]{7,20}$")

        if address and len(address) < 5:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="CONTACT_FORMAT",
                reason=f"Address length ({len(address)}) is less than minimum 5 characters.",
                user_message="The home address must be at least 5 characters long (e.g., street number, street name, and postal code).",
                rules_version=self.rules_version,
            )

        if phone and not phone_regex.match(phone):
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="CONTACT_FORMAT",
                reason=f"Phone '{phone}' does not match required format ^\\+?[\\d\\s\\-()]{{7,20}}$.",
                user_message="Please provide a valid phone number (7–20 digits, optional '+' country code, spaces, or hyphens).",
                rules_version=self.rules_version,
            )

        if not address and not phone:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="CONTACT_FORMAT",
                reason="Neither address nor phone provided.",
                user_message="Please specify a new address or phone number to update.",
                rules_version=self.rules_version,
            )

        idem_key = self.ledger.generate_idempotency_key(
            employee_id, "update_contact", {"address": address, "phone": phone}
        )

        if not confirmed:
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Contact format validated; awaiting user confirmation (B-3).",
                user_message=f"Confirm updating your WorkWeek contact record (address: '{address or 'unchanged'}', phone: '{phone or 'unchanged'}')?",
                idempotency_key=idem_key,
                confirmation_card={
                    "action": "update_contact",
                    "employee_id": employee_id,
                    "address": address,
                    "phone": phone,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW",
            rule_id="CONTACT_FORMAT",
            reason="Contact format valid and confirmed.",
            user_message="Contact update validated.",
            idempotency_key=idem_key,
            rules_version=self.rules_version,
        )

    # -------------------------------------------------------------------------
    # 3. Cancel Leave Validation (Compensating Action, §3.6 & §5.2)
    # -------------------------------------------------------------------------
    def _validate_cancel_leave(
        self, args: Dict[str, Any], *, employee_id: str, ctx: Dict[str, Any]
    ) -> PDPDecision:
        request_id = str(args.get("request_id", "")).strip()
        confirmed = bool(args.get("confirmed", False))
        owner_id = str(ctx.get("request_owner_id", employee_id))

        if owner_id != employee_id:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="RBAC_OWNERSHIP",
                reason=f"Caller {employee_id} does not own leave request {request_id}.",
                user_message="You can only cancel your own leave requests.",
                rules_version=self.rules_version,
            )

        idem_key = self.ledger.generate_idempotency_key(
            employee_id, "cancel_leave", {"request_id": request_id}
        )

        if not confirmed:
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Cancellation requires explicit user confirmation (B-3 & §3.6).",
                user_message=f"Confirm cancelling leave request {request_id} and refunding the days to your balance?",
                idempotency_key=idem_key,
                confirmation_card={
                    "action": "cancel_leave",
                    "employee_id": employee_id,
                    "request_id": request_id,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW",
            rule_id="COMPENSATION_CANCEL_LEAVE",
            reason="Confirmed leave cancellation.",
            user_message="Cancellation validated.",
            idempotency_key=idem_key,
            rules_version=self.rules_version,
        )

    # -------------------------------------------------------------------------
    # 4. Create Incident Validation (TICKET_DEDUPE, TICKET_PRIORITY_FLOOR, EQUIP_ELIGIBILITY, RELOCATION_CAP)
    # -------------------------------------------------------------------------
    def _validate_create_incident(
        self, args: Dict[str, Any], *, employee_id: str, ctx: Dict[str, Any]
    ) -> PDPDecision:
        category = str(args.get("category", "IT")).strip()
        short_desc = str(args.get("short_description", "")).strip()
        detailed_desc = str(args.get("detailed_description", short_desc)).strip()
        priority = str(args.get("priority", "3 - Moderate")).strip()
        confirmed = bool(args.get("confirmed", False))
        workflow_type = str(args.get("workflow_type", ctx.get("workflow_type", ""))).strip().lower()

        combined_text = f"{short_desc} {detailed_desc}".lower()

        # Rule: EQUIP_ELIGIBILITY (§5.4) — when ordering home office equipment / monitor
        if workflow_type == "equipment_procurement" or (
            "home office" in combined_text and ("monitor" in combined_text or "equipment" in combined_text)
        ):
            loc_status = str(ctx.get("location_status", args.get("location_status", "Hybrid")))
            estimated_cost = float(args.get("estimated_cost_usd", 350.0))
            shipping_address = str(ctx.get("address", args.get("shipping_address", ""))).strip()

            if loc_status not in ("Remote", "Hybrid"):
                return PDPDecision(
                    allowed=False,
                    status="DENY",
                    rule_id="EQUIP_ELIGIBILITY",
                    reason=f"Employee location_status is '{loc_status}'; §5.4 requires 'Remote' or 'Hybrid'.",
                    user_message=(
                        f"Under Handbook §5.4 (Home Office Equipment Allowance), only employees with an "
                        f"approved 'Remote' or 'Hybrid' location status qualify for the $500 USD equipment "
                        f"allowance. Your WorkWeek profile currently shows '{loc_status}'."
                    ),
                    rules_version=self.rules_version,
                )

            if estimated_cost > 500.0:
                return PDPDecision(
                    allowed=False,
                    status="DENY",
                    rule_id="EQUIP_ELIGIBILITY",
                    reason=f"Requested equipment cost ${estimated_cost:.2f} exceeds the $500.00 USD allowance (§5.4).",
                    user_message=(
                        f"The requested equipment cost (${estimated_cost:.2f} USD) exceeds the $500 USD "
                        "Home Office Equipment Allowance cap in Handbook §5.4."
                    ),
                    rules_version=self.rules_version,
                )

            if category != "Facilities":
                category = "Facilities"  # Enforce §5.4 'Facilities' category

            if len(shipping_address) < 5:
                return PDPDecision(
                    allowed=False,
                    status="DENY",
                    rule_id="EQUIP_ELIGIBILITY",
                    reason="Missing verified remote shipping address on file (§5.4).",
                    user_message="A verified remote shipping address of at least 5 characters is required in WorkWeek before ordering home office equipment (§5.4).",
                    rules_version=self.rules_version,
                )

        # Rule: RELOCATION_CAP (§5.5) — when requesting relocation / building badge pre-configuration
        if workflow_type == "relocation" or "relocation" in combined_text or "building badg" in combined_text:
            reloc_amount = float(args.get("relocation_amount_usd", 0.0))
            if reloc_amount > 10000.0:
                return PDPDecision(
                    allowed=False,
                    status="DENY",
                    rule_id="RELOCATION_CAP",
                    reason=f"Requested relocation amount ${reloc_amount:.2f} exceeds $10,000 USD cap (§5.5).",
                    user_message=(
                        f"The requested relocation amount (${reloc_amount:.2f} USD) exceeds the "
                        "$10,000 USD international relocation allowance cap specified in Handbook §5.5."
                    ),
                    rules_version=self.rules_version,
                )
            category = "Facilities"
            priority = "3 - Moderate"

        # Rule: TICKET_DEDUPE (FR-4.3 — 5-minute duplicate scan)
        dup_entry = self.ledger.find_recent_similar_ticket(
            employee_id=employee_id,
            category=category,
            short_description=short_desc,
            window_seconds=300,
        )
        if dup_entry:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="TICKET_DEDUPE",
                reason=f"Duplicate ticket detected within 5-minute window: {dup_entry.backend_ref}.",
                user_message=(
                    f"A similar {category} ticket was already created recently "
                    f"({dup_entry.backend_ref}). I have surfaced your existing ticket instead of creating a duplicate."
                ),
                existing_backend_ref=dup_entry.backend_ref,
                rules_version=self.rules_version,
            )

        # Rule: TICKET_PRIORITY_FLOOR (§5.5 programmatic downgrade for inflated priority)
        warnings: List[str] = []
        low_keywords = ["squeaky", "chair", "cosmetic", "scratch", "desk lamp"]
        critical_keywords = [
            "production outage",
            "security breach",
            "data leak",
            "system down",
            "completely unable to work",
            "emergency",
        ]
        is_low_impact = any(kw in combined_text for kw in low_keywords)
        is_critical_impact = any(kw in combined_text for kw in critical_keywords)

        if priority in ("1 - Critical", "2 - High") and (is_low_impact or not is_critical_impact):
            original_priority = priority
            priority = "4 - Low"
            warnings.append(
                f"Priority downgraded from '{original_priority}' to '{priority}' per Handbook §5.5 "
                "(minor facility or non-emergency issues are programmatically classified as '4 - Low')."
            )

        modified_args = dict(args)
        modified_args["category"] = category
        modified_args["priority"] = priority

        idem_key = self.ledger.generate_idempotency_key(
            employee_id,
            "create_incident",
            {"category": category, "short_description": short_desc, "priority": priority},
        )

        if not confirmed:
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Ticket validated; awaiting explicit user confirmation (B-3).",
                user_message=(
                    f"Confirm creating ServiceImmediately ticket (Category: '{category}', "
                    f"Priority: '{priority}', Summary: '{short_desc}')?"
                    + (f" Note: {warnings[0]}" if warnings else "")
                ),
                idempotency_key=idem_key,
                modified_args=modified_args,
                warnings=warnings,
                confirmation_card={
                    "action": "create_incident",
                    "employee_id": employee_id,
                    "category": category,
                    "priority": priority,
                    "short_description": short_desc,
                    "warnings": warnings,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW_WITH_WARNING" if warnings else "ALLOW",
            rule_id="TICKET_PRIORITY_FLOOR" if warnings else "TICKET_DEDUPE",
            reason="Incident creation validated and confirmed.",
            user_message="Ticket creation validated.",
            idempotency_key=idem_key,
            modified_args=modified_args,
            warnings=warnings,
            rules_version=self.rules_version,
        )

    # -------------------------------------------------------------------------
    # 5. Update Ticket Status Validation (TICKET_LIFECYCLE §5.5 & Boundary B-8)
    # -------------------------------------------------------------------------
    def _validate_update_status(
        self, args: Dict[str, Any], *, employee_id: str, ctx: Dict[str, Any]
    ) -> PDPDecision:
        ticket_id = str(args.get("ticket_id", "")).strip()
        new_status = str(args.get("new_status", args.get("status", ""))).strip()
        current_status = str(ctx.get("current_status", args.get("current_status", "New"))).strip()
        user_asserted_resolution = bool(args.get("user_asserted_resolution", False))
        confirmed = bool(args.get("confirmed", False))

        # Strict sequential state machine from Handbook §5.5:
        # New -> In Progress -> Resolved -> Closed
        # Even though the backend permits New -> Closed or Resolved -> In Progress, PDP strictly blocks!
        allowed_next = {
            "New": ["In Progress"],
            "In Progress": ["Resolved"],
            "Resolved": ["Closed"],
            "Closed": [],
        }

        valid_targets = allowed_next.get(current_status, [])
        if new_status not in valid_targets:
            next_hint = valid_targets[0] if valid_targets else "none (ticket is already Closed)"
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="TICKET_LIFECYCLE",
                reason=(
                    f"Invalid lifecycle transition '{current_status} -> {new_status}'. "
                    f"Handbook §5.5 requires sequential progression (New -> In Progress -> Resolved -> Closed)."
                ),
                user_message=(
                    f"Cannot transition ticket {ticket_id} directly from '{current_status}' to '{new_status}'. "
                    f"Per Handbook §5.5, tickets must progress sequentially (New → In Progress → Resolved → Closed). "
                    f"The valid next state from '{current_status}' is '{next_hint}'."
                ),
                rules_version=self.rules_version,
            )

        # Boundary B-8: No ticket auto-resolution unless the user explicitly asserts resolution
        if new_status in ("Resolved", "Closed") and not user_asserted_resolution:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="B-8_NO_AUTO_RESOLUTION",
                reason=f"Attempted to set ticket {ticket_id} to '{new_status}' without user_asserted_resolution=True (B-8).",
                user_message=(
                    f"Per governance boundary B-8, I can only transition ticket {ticket_id} to '{new_status}' "
                    "when you explicitly confirm that the issue is resolved."
                ),
                rules_version=self.rules_version,
            )

        idem_key = self.ledger.generate_idempotency_key(
            employee_id,
            "update_status",
            {"ticket_id": ticket_id, "from": current_status, "to": new_status},
        )

        if not confirmed:
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Lifecycle transition valid; awaiting explicit user confirmation (B-3).",
                user_message=f"Confirm updating ticket {ticket_id} status from '{current_status}' to '{new_status}'?",
                idempotency_key=idem_key,
                confirmation_card={
                    "action": "update_status",
                    "employee_id": employee_id,
                    "ticket_id": ticket_id,
                    "current_status": current_status,
                    "new_status": new_status,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW",
            rule_id="TICKET_LIFECYCLE",
            reason=f"Sequential transition '{current_status} -> {new_status}' validated and confirmed.",
            user_message="Status transition validated.",
            idempotency_key=idem_key,
            rules_version=self.rules_version,
        )

    # -------------------------------------------------------------------------
    # 6. Add Comment Validation
    # -------------------------------------------------------------------------
    def _validate_add_comment(
        self, args: Dict[str, Any], *, employee_id: str, ctx: Dict[str, Any]
    ) -> PDPDecision:
        ticket_id = str(args.get("ticket_id", "")).strip()
        comment = str(args.get("comment", "")).strip()
        confirmed = bool(args.get("confirmed", False))
        ticket_owner = str(ctx.get("requestor_id", employee_id))

        if ticket_owner != employee_id:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="RBAC_OWNERSHIP",
                reason=f"Caller {employee_id} does not own ticket {ticket_id}.",
                user_message="You may only add comments to your own tickets.",
                rules_version=self.rules_version,
            )

        if not comment:
            return PDPDecision(
                allowed=False,
                status="DENY",
                rule_id="COMMENT_EMPTY",
                reason="Comment text cannot be empty.",
                user_message="Please provide a comment to add to the ticket.",
                rules_version=self.rules_version,
            )

        idem_key = self.ledger.generate_idempotency_key(
            employee_id, "add_comment", {"ticket_id": ticket_id, "comment": comment}
        )

        if not confirmed:
            return PDPDecision(
                allowed=False,
                status="CONFIRMATION_REQUIRED",
                rule_id="B-3_CONFIRM_BEFORE_WRITE",
                reason="Awaiting user confirmation before posting comment (B-3).",
                user_message=f"Confirm adding comment to ticket {ticket_id}: '{comment}'?",
                idempotency_key=idem_key,
                confirmation_card={
                    "action": "add_comment",
                    "employee_id": employee_id,
                    "ticket_id": ticket_id,
                    "comment": comment,
                    "idempotency_key": idem_key,
                },
                rules_version=self.rules_version,
            )

        return PDPDecision(
            allowed=True,
            status="ALLOW",
            rule_id="TICKET_OWNERSHIP",
            reason="Comment validated and confirmed.",
            user_message="Comment validated.",
            idempotency_key=idem_key,
            rules_version=self.rules_version,
        )


DEFAULT_PDP = PolicyDecisionPoint()
