from tests.lawyer_workbench._support.flow_runner import auto_answer_card, is_session_busy_sse


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
