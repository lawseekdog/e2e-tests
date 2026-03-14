import pytest

from tests.lawyer_workbench._support.flow_runner import (
    WorkbenchFlow,
    auto_answer_card,
    is_session_busy_sse,
)


def test_auto_answer_card_ignores_inferred_fields_not_in_questions():
    card = {
        "skill_id": "skill-error-analysis",
        "prompt": (
            "技能 evidence-elements-extraction 执行失败：请基于 evidence_analysis 至少产出 1 条 "
            "evidence_elements.items（含 file_id/element_type/value/quote）。"
        ),
        "questions": [
            {
                "field_key": "data.workbench.skill_error_acknowledged",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []

    assert len(answers) == 1
    assert answers[0]["field_key"] == "data.workbench.skill_error_acknowledged"
    assert answers[0]["value"] is True


def test_auto_answer_card_does_not_infer_answers_when_questions_empty():
    card = {
        "skill_id": "skill-error-analysis",
        "prompt": "缺口字段: ['profile.review_scope']",
        "questions": [],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    assert user_response == {"answers": []}


def test_auto_answer_card_keeps_evidence_gap_stop_ask_false_by_default():
    card = {
        "skill_id": "evidence-gap-clarify",
        "questions": [
            {
                "field_key": "data.evidence.evidence_gap_stop_ask",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    assert answers and answers[0]["field_key"] == "data.evidence.evidence_gap_stop_ask"
    assert answers[0]["value"] is False


def test_auto_answer_card_keeps_regenerate_documents_false_by_default():
    card = {
        "skill_id": "documents-stale",
        "questions": [
            {
                "field_key": "data.work_product.regenerate_documents",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    assert answers and answers[0]["field_key"] == "data.work_product.regenerate_documents"
    assert answers[0]["value"] is False


def test_auto_answer_card_select_uses_option_id_when_value_missing():
    card = {
        "skill_id": "intent-route-v3",
        "questions": [
            {
                "field_key": "profile.client_role",
                "input_type": "select",
                "required": True,
                "options": [
                    {"id": "plaintiff", "label": "原告"},
                    {"id": "defendant", "label": "被告"},
                ],
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    assert answers and answers[0]["field_key"] == "profile.client_role"
    assert answers[0]["value"] == "plaintiff"


def test_auto_answer_card_does_not_add_top_level_alias_when_not_asked():
    card = {
        "skill_id": "intent-route-v3",
        "questions": [
            {
                "field_key": "profile.client_role",
                "input_type": "select",
                "required": True,
                "options": [{"value": "applicant", "label": "申请人"}],
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.client_role") == "applicant"
    assert "client_role" not in by_key


def test_auto_answer_card_select_respects_profile_client_role_override():
    card = {
        "skill_id": "intent-route-v3",
        "questions": [
            {
                "field_key": "profile.client_role",
                "input_type": "select",
                "required": True,
                "options": [
                    {"id": "plaintiff", "label": "原告"},
                    {"id": "appellee", "label": "被上诉人"},
                ],
            }
        ],
    }

    user_response = auto_answer_card(
        card,
        overrides={"profile.client_role": "appellee"},
        uploaded_file_ids=[],
    )
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.client_role") == "appellee"
    assert "client_role" not in by_key


def test_auto_answer_card_adds_alias_only_when_alias_field_is_asked():
    card = {
        "skill_id": "intent-route-v3",
        "questions": [
            {
                "field_key": "profile.client_role",
                "input_type": "select",
                "required": True,
                "options": [{"value": "appellee", "label": "被上诉人"}],
            },
            {
                "field_key": "client_role",
                "input_type": "select",
                "required": True,
                "options": [{"value": "appellee", "label": "被上诉人"}],
            },
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.client_role") == "appellee"
    assert by_key.get("client_role") == "appellee"


def test_auto_answer_card_normalizes_contract_review_scope_values():
    card = {
        "skill_id": "contract-intake",
        "questions": [
            {
                "field_key": "profile.review_scope",
                "input_type": "select",
                "required": True,
                "options": [
                    {"label": "全面审查", "value": "全面审查"},
                    {"label": "重点条款审查", "value": "重点条款审查"},
                ],
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.review_scope") == "全面审查"


def test_auto_answer_card_normalizes_review_scope_override():
    card = {
        "skill_id": "contract-intake",
        "questions": [
            {
                "field_key": "profile.review_scope",
                "input_type": "select",
                "required": True,
                "options": [
                    {"label": "全面审查", "value": "全面审查"},
                    {"label": "重点条款审查", "value": "重点条款审查"},
                ],
            }
        ],
    }

    user_response = auto_answer_card(
        card,
        overrides={"profile.review_scope": "重点条款审查"},
        uploaded_file_ids=[],
    )
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.review_scope") == "重点条款审查"


def test_is_session_busy_sse_detects_busy_error_message():
    sse = {
        "events": [
            {
                "event": "error",
                "data": {"message": "当前会话正在处理中，请等待上一轮完成后再发送。"},
            }
        ]
    }
    assert is_session_busy_sse(sse) is True


def test_is_session_busy_sse_detects_background_processing_hint():
    sse = {
        "events": [
            {
                "event": "error",
                "data": {"message": "后台继续处理中，请刷新查看待办。", "partial": True},
            }
        ],
        "output": "后台继续处理中，请刷新查看待办。",
    }
    assert is_session_busy_sse(sse) is True


def test_is_session_busy_sse_detects_partial_stream_timeout():
    sse = {
        "events": [
            {
                "event": "error",
                "data": {"error": "stream_timeout", "partial": True, "timeout_s": 180},
            }
        ]
    }
    assert is_session_busy_sse(sse) is True


def test_auto_answer_card_select_prefers_positive_confirmation_option():
    card = {
        "skill_id": "contract-review",
        "questions": [
            {
                "field_key": "data.workbench.contract_review_confirm",
                "input_type": "select",
                "required": True,
                "options": [
                    {"value": "regenerate", "label": "返回重新生成"},
                    {"value": "continue", "label": "继续推进并生成交付物"},
                ],
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("data.workbench.contract_review_confirm") == "continue"


def test_auto_answer_card_falls_back_to_supported_document_type():
    card = {
        "skill_id": "document-drafting-intake",
        "questions": [
            {
                "field_key": "profile.document_type",
                "input_type": "text",
                "required": True,
            }
        ],
    }
    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.document_type") == "民事起诉状"


def test_auto_answer_card_prefers_court_name_for_court_question_even_when_field_key_reused():
    card = {
        "skill_id": "document-drafting-intake",
        "questions": [
            {
                "field_key": "profile.background",
                "question": "您希望这份起诉状用于哪个法院？请提供具体法院名称。",
                "input_type": "textarea",
                "required": True,
            }
        ],
    }
    user_response = auto_answer_card(
        card,
        overrides={"profile.background": "这是一段很长的案情背景，不能直接当作法院名称。"},
        uploaded_file_ids=[],
    )
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("profile.background") == "北京市海淀区人民法院"


def test_auto_answer_card_doc_draft_recovery_skips_generic_doc_generation_card():
    card = {
        "skill_id": "skill-error-analysis",
        "task_key": "workflow_confirm_doc_generation_skill-error-analysis",
        "prompt": "文书生成失败，请确认后重试。",
        "questions": [
            {
                "field_key": "data.workbench.skill_error_acknowledged",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}

    assert by_key.get("data.workbench.skill_error_acknowledged") is True
    assert "data.work_product.document_drafts" not in by_key
    assert "data.work_product.drafts_ready" not in by_key


def test_auto_answer_card_doc_draft_recovery_skips_generic_doc_draft_card_without_targets():
    card = {
        "skill_id": "skill-error-analysis",
        "task_key": "workflow_confirm_doc_draft_skill-error-analysis",
        "prompt": "文书草稿生成失败，请确认后重试。",
        "questions": [
            {
                "field_key": "data.workbench.skill_error_acknowledged",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}

    assert by_key.get("data.workbench.skill_error_acknowledged") is True
    assert "data.work_product.document_drafts" not in by_key
    assert "data.work_product.drafts_ready" not in by_key


def test_auto_answer_card_doc_draft_recovery_keeps_contract_targets_when_prompt_has_template_ids():
    card = {
        "skill_id": "skill-error-analysis",
        "task_key": "workflow_confirm_doc_generation_skill-error-analysis",
        "prompt": (
            "请修复并继续：document_drafts="
            "contract_review_report(215), modification_suggestion(270), redline_comparison(277)"
        ),
        "questions": [
            {
                "field_key": "data.workbench.skill_error_acknowledged",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=[])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}

    drafts = by_key.get("data.work_product.document_drafts")
    assert isinstance(drafts, list)
    assert len(drafts) == 3
    assert {str(item.get("output_key") or "") for item in drafts if isinstance(item, dict)} == {
        "contract_review_report",
        "modification_suggestion",
        "redline_comparison",
    }
    assert by_key.get("data.work_product.drafts_ready") is True


def test_auto_answer_card_completion_question_maps_to_positive_select_option():
    card = {
        "skill_id": "materials-intake",
        "questions": [
            {
                "field_key": "data.materials.upload_completed",
                "question": "是否已完成所有材料上传？如已完成请勾选。",
                "input_type": "select",
                "required": True,
                "options": [
                    {"value": "pending", "label": "否，继续上传"},
                    {"value": "done", "label": "是，已完成上传"},
                ],
            }
        ],
    }
    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=["f1"])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}
    assert by_key.get("data.materials.upload_completed") == "done"


def test_auto_answer_card_optional_file_ids_never_uses_text_value():
    card = {
        "skill_id": "file-insight",
        "questions": [
            {
                "field_key": "attachment_file_ids",
                "question": "请上传补充材料或说明无法解析的材料内容（如关键截图/文字说明等）",
                "input_type": "file_ids",
                "required": False,
            },
            {
                "field_key": "data.files.preprocess_stop_ask",
                "question": "是否已完成所有材料上传？如已完成，请勾选此项，我们将基于现有材料继续分析",
                "input_type": "boolean",
                "required": True,
            },
        ],
    }

    user_response = auto_answer_card(card, overrides={}, uploaded_file_ids=["f1", "f2"])
    answers = user_response.get("answers") or []
    by_key = {row["field_key"]: row["value"] for row in answers}

    attachment_value = by_key.get("attachment_file_ids")
    assert attachment_value is None or isinstance(attachment_value, list)
    assert by_key.get("data.files.preprocess_stop_ask") is True


@pytest.mark.asyncio
async def test_resume_card_allows_busy_partial_stream_without_user_message():
    class _FakeClient:
        async def resume(self, session_id, user_response, pending_card, max_loops):  # noqa: ANN001
            _ = (session_id, user_response, pending_card, max_loops)
            return {
                "events": [
                    {
                        "event": "error",
                        "data": {"error": "stream_timeout", "partial": True, "timeout_s": 30},
                    }
                ],
                "output": "",
            }

    flow = WorkbenchFlow(client=_FakeClient(), session_id="s-test")
    card = {
        "skill_id": "documents-stale",
        "questions": [
            {
                "field_key": "data.work_product.regenerate_documents",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    sse = await flow.resume_card(card)
    assert is_session_busy_sse(sse) is True


@pytest.mark.asyncio
async def test_resume_card_honors_explicit_max_loops_override():
    captured: dict[str, object] = {}

    class _FakeClient:
        async def resume(self, session_id, user_response, pending_card, max_loops):  # noqa: ANN001
            _ = (session_id, user_response, pending_card)
            captured["max_loops"] = max_loops
            return {"events": [{"event": "user_message", "data": {"role": "user", "content": "ok"}}], "output": ""}

    flow = WorkbenchFlow(client=_FakeClient(), session_id="s-test")
    card = {
        "skill_id": "documents-stale",
        "questions": [
            {
                "field_key": "data.work_product.regenerate_documents",
                "input_type": "boolean",
                "required": True,
            }
        ],
    }

    await flow.resume_card(card, max_loops=12)
    assert captured.get("max_loops") == 12
