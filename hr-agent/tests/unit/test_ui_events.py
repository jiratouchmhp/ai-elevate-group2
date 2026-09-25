"""Unit tests for the ADK -> AG-UI translator (app/ui_events.py)."""

from types import SimpleNamespace as NS

from app.ui_events import RUN_ERROR_MESSAGE, AguiTranslator


def _ev(author="hr_agent", partial=False, parts=()):
    return NS(author=author, partial=partial, content=NS(parts=list(parts)))


def _text(t, thought=False):
    return NS(text=t, thought=thought, function_call=None, function_response=None)


def _call(cid, name, **args):
    return NS(text=None, function_call=NS(id=cid, name=name, args=args), function_response=None)


def _resp(cid, name, response):
    return NS(text=None, function_call=None, function_response=NS(id=cid, name=name, response=response))


def test_partial_stream_is_not_duplicated_by_final_event():
    tr = AguiTranslator("t1")
    out = tr.translate(_ev(partial=True, parts=[_text("Hel")]))
    out += tr.translate(_ev(partial=True, parts=[_text("lo")]))
    out += tr.translate(_ev(partial=False, parts=[_text("Hello")]))
    assert [e["type"] for e in out] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CONTENT",
                                        "TEXT_MESSAGE_END"]
    assert "".join(e.get("delta", "") for e in out) == "Hello"


def test_screened_final_replaces_streamed_text():
    """Model Armor screens only the final response: a changed final supersedes the partials."""
    tr = AguiTranslator("t1")
    out = tr.translate(_ev(partial=True, parts=[_text("unsafe ")]))
    out += tr.translate(_ev(partial=True, parts=[_text("draft")]))
    out += tr.translate(_ev(partial=False, parts=[_text("I can't help with that request.")]))
    types = [e["type"] for e in out]
    assert types[-2:] == ["CUSTOM", "TEXT_MESSAGE_END"]
    rep = out[-2]
    assert rep["name"] == "message_replace" and rep["value"]["text"] == "I can't help with that request."
    assert rep["value"]["messageId"] == out[0]["messageId"]
    # An unchanged final after a new message does not replace anything.
    out = tr.translate(_ev(partial=True, parts=[_text("ok")])) + tr.translate(_ev(parts=[_text("ok")]))
    assert not any(e.get("name") == "message_replace" for e in out)


def test_non_streamed_text_and_filters():
    tr = AguiTranslator("t1")
    out = tr.translate(_ev(parts=[_text("thinking", thought=True), _text("Hi")]))
    out += tr.translate(_ev(author="policy_agent", parts=[_text("draft")]))
    assert [e.get("delta") for e in out if e["type"] == "TEXT_MESSAGE_CONTENT"] == ["Hi"]


def test_proposal_card_and_b3_denial_keeps_card():
    tr = AguiTranslator("t1")
    tr.translate(_ev(author="workweek_agent", parts=[_call("c1", "propose_leave", start_date="2026-11-02")]))
    out = tr.translate(_ev(author="workweek_agent", parts=[_resp("c1", "propose_leave", {
        "status": "awaiting_confirmation", "action": "submit_leave", "proposal_id": "P1-abc",
        "proposed": {"start_date": "2026-11-02", "days": 2}, "warnings": ["w"], "computed": {"balance_after": 3}})]))
    end, delta = out
    assert end["type"] == "TOOL_CALL_END" and end["status"] == "awaiting_confirmation"
    assert delta["delta"][0]["path"] == "/pending_proposals/P1-abc"
    assert delta["delta"][0]["value"]["computed"] == {"balance_after": 3}

    tr.translate(_ev(author="workweek_agent", parts=[_call("c2", "commit_action", proposal_id="P1-abc")]))
    out = tr.translate(_ev(author="workweek_agent", parts=[_resp("c2", "commit_action", {
        "status": "denied", "rule_ids": ["B3_CONFIRMATION_REQUIRED"], "reasons": ["confirm first"]})]))
    assert [e["type"] for e in out] == ["TOOL_CALL_END", "CUSTOM"]  # no STATE_DELTA remove


def test_commit_receipt_removes_card_and_citations_dedupe():
    tr = AguiTranslator("t1")
    tr.translate(_ev(parts=[_call("c2", "commit_action", proposal_id="P1-abc")]))
    out = tr.translate(_ev(parts=[_resp("c2", "commit_action", {"status": "committed", "backend_ref": "LR-9"})]))
    assert out[1]["value"]["backend_ref"] == "LR-9"
    assert out[2]["delta"] == [{"op": "remove", "path": "/pending_proposals/P1-abc"}]
    res = {"sufficient": True, "results": [{"anchor": "a1", "citation": "§2 Leave"}, {"anchor": "a1"}]}
    cites = [e for e in tr.translate(_ev(parts=[_resp("c3", "search_policy", res)])) if e["type"] == "CUSTOM"]
    assert len(cites) == 1 and cites[0]["value"]["label"] == "§2 Leave"


def test_error_and_guardrail_events():
    tr = AguiTranslator("t1")
    tr.translate(_ev(partial=True, parts=[_text("par")]))
    err = tr.error()
    assert [e["type"] for e in err] == ["TEXT_MESSAGE_END", "RUN_ERROR"]
    assert err[-1]["message"] == RUN_ERROR_MESSAGE and "Traceback" not in err[-1]["message"]
    g = tr.guardrail_block({"tool_invoked": "input_screen", "guardrail_verdicts": [{"categories": ["self_harm"]}]})
    assert g[0]["value"]["wellbeing"] is True


def _widget_for(name, data, author="workweek_agent"):
    tr = AguiTranslator("t1")
    start = tr.translate(_ev(author=author, parts=[_call("w1", name)]))
    out = tr.translate(_ev(author=author, parts=[_resp("w1", name, {"status": "success", "data": data})]))
    return start, [e for e in out if e["type"] == "CUSTOM" and e["name"] == "widget"]


def test_tool_events_carry_flow_system():
    start, _ = _widget_for("get_leave_balance", {})
    assert start[0]["system"] == "workweek"
    tr = AguiTranslator("t1")
    assert tr.translate(_ev(parts=[_call("a", "policy_agent")]))[0]["system"] == "policy_agent"
    assert tr.translate(_ev(author="itsm_agent", parts=[_call("b", "commit_action")]))[0]["system"] == "itsm"
    assert tr.translate(_ev(author="policy_agent", parts=[_call("c", "search_policy")]))[0]["system"] == "handbook"


def test_leave_balance_widget():
    _, w = _widget_for("get_leave_balance", {"Vacation": {"accrued": 21, "used": 16, "remaining": 5},
                                             "Sick": {"accrued": 14, "used": 2}})
    assert w[0]["value"]["kind"] == "leave_balance"
    bal = {b["type"]: b for b in w[0]["value"]["data"]["balances"]}
    assert bal["Vacation"]["remaining"] == 5 and bal["Sick"]["remaining"] == 12


def test_profile_widget_drops_spii():
    _, w = _widget_for("get_profile", {"name": "Alex Tan", "role": "Engineer", "email": "a@x.com",
                                       "address": "12 Marina Blvd", "phone": "+65 9123 4567",
                                       "nric": "S1234567D", "location_status": "Hybrid"})
    data = w[0]["value"]["data"]
    assert data == {"name": "Alex Tan", "role": "Engineer", "location_status": "Hybrid"}


def test_personal_info_never_becomes_a_widget():
    _, w = _widget_for("get_personal_info", {"address": "12 Marina Blvd", "phone": "+65 9123 4567"})
    assert w == []


def test_ticket_widgets_redact_free_text_and_tolerate_shapes():
    _, w = _widget_for("list_tickets", {"tickets": [{"ticket_id": "INC1", "short_description": "call 91234567",
                                                     "status": "New", "requestor_email": "a@x.com"}]},
                       author="itsm_agent")
    t = w[0]["value"]["data"]["tickets"][0]
    assert "91234567" not in t["short_description"] and "requestor_email" not in t
    _, w = _widget_for("get_ticket", {"ticket_id": "INC1", "requestor_id": "EMP001", "status": "New",
                                      "description": "My NRIC is S1234567D",
                                      "comments": [{"author": "EMP001", "actor_type": "HUMAN", "text": "hi"},
                                                   {"author": "x", "actor_type": "AUTOMATED_AGENT", "text": "ok"}]},
                       author="itsm_agent")
    d = w[0]["value"]["data"]
    assert "S1234567D" not in d["description"]
    assert [c["by"] for c in d["comments"]] == ["you", "assistant"]


def test_failed_or_malformed_reads_emit_no_widget():
    tr = AguiTranslator("t1")
    tr.translate(_ev(parts=[_call("x", "list_tickets")]))
    out = tr.translate(_ev(parts=[_resp("x", "list_tickets", {"status": "unavailable", "message": "down"})]))
    assert [e["type"] for e in out] == ["TOOL_CALL_END"]
    _, w = _widget_for("get_ticket", "garbage", author="itsm_agent")
    assert w == []
