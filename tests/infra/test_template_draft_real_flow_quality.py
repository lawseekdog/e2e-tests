from __future__ import annotations

from scripts.run_template_draft_real_flow import _evaluate_document_quality, _sse_has_user_message_event


def test_evaluate_document_quality_rejects_unresolved_and_generic_legal_wording() -> None:
    text = (
        "民事起诉状\n"
        "诉讼请求：请求判令被告返还借款本金并承担诉讼费。\n"
        "事实与理由：以下仅作为输出格式示例，具体法院名称待核实。\n"
        "管辖与法律依据：根据相关法律规定，第六百七十六条规定应承担逾期利息。"
    )

    result = _evaluate_document_quality(
        text=text,
        targets={"parties": [], "amounts": [], "claim_keywords": []},
        min_citations=0,
        deliverable_status="archived",
        strict_quality=True,
    )

    assert result.get("pass") is False
    reasons = "；".join(result.get("failure_reasons") or [])
    assert "未完成占位/待核实表述" in reasons
    assert "泛化法律表述" in reasons
    assert "未指明法名" in reasons


def test_evaluate_document_quality_accepts_grounded_legal_wording() -> None:
    text = (
        "民事起诉状\n"
        "诉讼请求：请求判令被告返还借款本金并承担诉讼费。\n"
        "事实与理由：被告逾期未还款，原告多次催收无果。\n"
        "管辖与法律依据：依据《中华人民共和国民法典》第六百七十六条规定，应当支付逾期利息。"
    )

    result = _evaluate_document_quality(
        text=text,
        targets={"parties": [], "amounts": [], "claim_keywords": []},
        min_citations=1,
        deliverable_status="archived",
        strict_quality=True,
    )

    assert result.get("pass") is True
    assert result.get("failure_reasons") == []


def test_sse_has_user_message_event_detects_event() -> None:
    sse = {
        "events": [
            {"event": "progress", "data": {"step": "drafting"}},
            {"event": "user_message", "data": {"content": "ok"}},
        ]
    }

    assert _sse_has_user_message_event(sse) is True


def test_sse_has_user_message_event_returns_false_without_user_message() -> None:
    sse = {
        "events": [
            {"event": "progress", "data": {"step": "drafting"}},
            {"event": "end", "data": {}},
        ]
    }

    assert _sse_has_user_message_event(sse) is False
