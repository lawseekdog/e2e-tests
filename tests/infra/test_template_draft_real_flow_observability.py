from __future__ import annotations

from scripts.run_template_draft_real_flow import (
    DEFAULT_FACTS,
    _build_doc_targets,
    _build_node_timeline_row,
    _detect_docgen_node,
    _is_stop_node_reached,
)


def test_detect_docgen_node_prefers_current_task_id() -> None:
    node = _detect_docgen_node(
        current_task_id="section_contract",
        current_phase="docgen",
        pending_card={},
        deliverable={},
        docgen={"section_contract_ready": True, "hard_validated": True},
        trace_node_ids=["document-generation"],
        template_quality_contracts_json_exists=True,
        docgen_repair_plan_exists=False,
        quality_review_decision="",
    )

    assert node == "section_contract"



def test_detect_docgen_node_falls_back_to_docgen_flags() -> None:
    node = _detect_docgen_node(
        current_task_id="",
        current_phase="docgen",
        pending_card={},
        deliverable={},
        docgen={
            "section_contract_ready": True,
            "hard_validated": True,
            "soft_validated": False,
            "repair_required": False,
            "rendered": False,
            "synced": False,
        },
        trace_node_ids=[],
        template_quality_contracts_json_exists=False,
        docgen_repair_plan_exists=False,
        quality_review_decision="",
    )

    assert node == "soft_validate"



def test_build_default_legal_opinion_targets() -> None:
    targets = _build_doc_targets(DEFAULT_FACTS)

    assert "北京云杉科技有限公司" in (targets.get("parties") or [])
    assert "上海启衡数据系统有限公司" in (targets.get("parties") or [])
    assert "360000" in (targets.get("amounts") or [])
    assert "252000" in (targets.get("amounts") or [])
    assert "评估" in (targets.get("claim_keywords") or [])
    assert "建议" in (targets.get("claim_keywords") or [])



def test_stop_after_node_matches_finish() -> None:
    assert (
        _is_stop_node_reached(
            target_node="finish",
            current_node="finish",
            seen_nodes=["intake", "section_contract", "compose", "render", "sync", "finish"],
        )
        is True
    )



def test_timeline_row_contains_repair_plan_flags() -> None:
    row = _build_node_timeline_row(
        step=7,
        trigger="poll.loop",
        observed_at="2026-03-08T12:00:00",
        docgen_snapshot={
            "matter_id": "9001",
            "session_id": "1888",
            "current_phase": "docgen",
            "current_task_id": "docgen_repair",
            "docgen_node": "repair",
            "pending_card": {
                "skill_id": "document-repair",
                "task_key": "repair_review",
                "review_type": "clarify",
            },
            "deliverable": {"status": "draft", "file_id": ""},
            "template_quality_contracts_json_exists": True,
            "docgen_repair_plan_exists": True,
            "docgen_repair_contracts_json_exists": True,
            "quality_review_decision": "repair",
            "soft_reason_codes": ["low_signal_phrase_detected"],
            "documents_fingerprint": "docs-fp-1",
            "quality_review_fingerprint": "qr-fp-1",
            "docgen": {
                "section_contract_ready": True,
                "hard_validated": True,
                "soft_validated": False,
                "repair_required": True,
                "rendered": False,
                "synced": False,
                "repair_round": 1,
            },
            "trace": {"latest_docgen_node_id": "document-repair"},
        },
        docgen_node_sequence=["intake", "section_contract", "compose", "hard_validate", "soft_validate", "repair"],
    )

    assert row["docgen_node"] == "repair"
    assert row["docgen_repair_plan_exists"] is True
    assert row["docgen_repair_contracts_json_exists"] is True
    assert row["template_quality_contracts_json_exists"] is True
    assert row["quality_review_decision"] == "repair"
    assert row["docgen_flags"]["repair_required"] is True
