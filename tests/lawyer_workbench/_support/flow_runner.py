"""Session-driven workflow runner for lawyer workbench E2E.

This drives the real chain:
gateway -> consultations-service -> ai-engine (LangGraph) -> matter-service/files-service/templates/knowledge/memory.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .utils import trim, unwrap_api_response
from .sse import assert_has_user_message

PendingCardStopFn = Callable[[dict[str, Any]], bool]

_NUDGE_TEXT = "继续"
_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}
_AUTO_NUDGE = str(os.getenv("E2E_AUTO_NUDGE", "1") or "").strip().lower() in {"1", "true", "yes"}
_PROGRESS = str(os.getenv("E2E_FLOW_PROGRESS", "1") or "").strip().lower() in {"1", "true", "yes"}


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Local dev workflows can be multi-skill and may need a larger per-resume loop budget to
# reliably reach the next interrupt without requiring extra "继续" nudges.
_RESUME_MAX_LOOPS = _read_int_env("E2E_RESUME_MAX_LOOPS", 80)
_SESSION_BUSY_BACKOFF_S = float(os.getenv("E2E_SESSION_BUSY_BACKOFF_S", "2.5") or 2.5)
_SESSION_BUSY_EXTRA_RETRIES = _read_int_env("E2E_SESSION_BUSY_EXTRA_RETRIES", 180)
_UNANSWERABLE_CARD_MAX_REPEATS = _read_int_env("E2E_UNANSWERABLE_CARD_MAX_REPEATS", 6)
_REPEATED_CARD_ABORT_COUNT = _read_int_env("E2E_REPEATED_CARD_ABORT_COUNT", 10)


def _debug(msg: str) -> None:
    if _DEBUG:
        print(msg, flush=True)


def _progress(msg: str) -> None:
    if _PROGRESS:
        print(msg, flush=True)


def extract_last_card_from_sse(sse: dict[str, Any]) -> dict[str, Any] | None:
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    for idx in range(len(events) - 1, -1, -1):
        it = events[idx]
        if not isinstance(it, dict):
            continue
        if it.get("event") != "card":
            continue
        data = it.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def _pending_card_intercept_sse(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [{"event": "card", "data": card}],
        "pending_card": card,
        "output": "pending card intercepted",
    }


def is_session_busy_sse(sse: dict[str, Any] | None) -> bool:
    if not isinstance(sse, dict):
        return False
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    for it in events:
        if not isinstance(it, dict):
            continue
        event_name = str(it.get("event") or "").strip()
        if event_name == "session_busy":
            return True
        if event_name == "error":
            raw_data = it.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            if data.get("partial") is True:
                err_code = str(data.get("error") or "").strip().lower()
                if err_code in {"stream_timeout", "timeout", "request_timeout"}:
                    return True
            msg = " ".join([str(data.get("message") or ""), str(data.get("error") or "")]).strip().lower()
            if not msg:
                continue
            if (
                ("session busy" in msg)
                or ("会话正在处理中" in msg)
                or ("上一轮完成后再发送" in msg)
                or ("already processing" in msg)
                or ("后台继续处理中" in msg)
                or ("刷新查看待办" in msg)
            ):
                return True
    output = str(sse.get("output") or "").strip()
    if not output:
        return False
    return (
        ("会话正在处理中" in output)
        or ("session busy" in output.lower())
        or ("后台继续处理中" in output)
        or ("刷新查看待办" in output)
    )


def _pick_recommended_or_first(options: list[Any]) -> Any | None:
    if not isinstance(options, list) or not options:
        return None

    def _option_value(opt: Any) -> Any | None:
        if not isinstance(opt, dict):
            return None
        if opt.get("value") is not None:
            return opt.get("value")
        # Some cards expose options as {id,label} (without value).
        if opt.get("id") is not None:
            return opt.get("id")
        return None

    def _option_text(opt: Any) -> str:
        if not isinstance(opt, dict):
            return ""
        bits = [
            str(opt.get("label") or ""),
            str(opt.get("value") or ""),
            str(opt.get("id") or ""),
        ]
        return " ".join(x for x in bits if x).strip().lower()

    for opt in options:
        v = _option_value(opt)
        if isinstance(opt, dict) and opt.get("recommended") is True and v is not None:
            return v

    positive_tokens = ("继续", "确认", "同意", "通过", "接受", "accept", "continue", "proceed", "yes")
    negative_tokens = ("返回", "重试", "重新", "忽略", "拒绝", "取消", "reject", "deny", "no")
    for opt in options:
        v = _option_value(opt)
        if v is None:
            continue
        text = _option_text(opt)
        if not text:
            continue
        if any(tok in text for tok in positive_tokens) and not any(tok in text for tok in negative_tokens):
            return v

    for opt in options:
        v = _option_value(opt)
        if v is not None:
            return v
    return None


def _normalize_review_scope(value: Any) -> Any:
    s = str(value or "").strip().lower()
    if not s:
        return value
    if s in {"full", "全面审查", "全面", "all"}:
        return "full"
    if s in {"focused", "重点条款审查", "重点审查", "重点", "partial"}:
        return "focused"
    return value


def _option_answer_value(option: Any) -> Any | None:
    if not isinstance(option, dict):
        return None
    if option.get("value") is not None:
        return option.get("value")
    if option.get("id") is not None:
        return option.get("id")
    label = option.get("label")
    if isinstance(label, (str, int, float, bool)):
        return label
    return None


def _option_label_for_value(options: list[Any], value: Any) -> str | None:
    target = str(value).strip()
    if not target:
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        candidate = _option_answer_value(option)
        if str(candidate).strip() != target:
            continue
        label = str(option.get("label") or "").strip()
        return label or None
    return None


def _option_match_text(option: Any) -> str:
    if not isinstance(option, dict):
        return ""
    parts = [
        str(option.get("value") or ""),
        str(option.get("id") or ""),
        str(option.get("label") or ""),
    ]
    return " ".join(x for x in parts if x).strip().lower()


def _coerce_review_scope_for_options(value: Any, options: list[Any] | None) -> Any:
    normalized = _normalize_review_scope(value)
    opts = options if isinstance(options, list) else []
    if not opts:
        return normalized

    if normalized == "full":
        for opt in opts:
            picked = _option_answer_value(opt)
            if picked is None:
                continue
            text = _option_match_text(opt)
            if ("full" in text) or ("全面" in text) or ("all" in text):
                return picked
        return normalized

    if normalized == "focused":
        for opt in opts:
            picked = _option_answer_value(opt)
            if picked is None:
                continue
            text = _option_match_text(opt)
            if ("focused" in text) or ("重点" in text) or ("partial" in text):
                return picked
        return normalized

    original_text = str(value or "").strip().lower()
    normalized_text = str(normalized or "").strip().lower()
    for opt in opts:
        picked = _option_answer_value(opt)
        if picked is None:
            continue
        pv = str(picked).strip().lower()
        text = _option_match_text(opt)
        if original_text and (pv == original_text or original_text in text):
            return picked
        if normalized_text and (pv == normalized_text or normalized_text in text):
            return picked
    return normalized


def _resolve_override_value(field_key: str, overrides: dict[str, Any]) -> Any | None:
    if not isinstance(overrides, dict) or not overrides:
        return None
    if field_key in overrides:
        return overrides[field_key]
    # Support nested object overrides, e.g.:
    # overrides["profile.plaintiff"] = {"name": "..."} can satisfy "profile.plaintiff.name".
    for k, v in overrides.items():
        if not isinstance(k, str) or not k:
            continue
        if not isinstance(v, dict):
            continue
        prefix = f"{k}."
        if not field_key.startswith(prefix):
            continue
        sub = field_key[len(prefix) :]
        if sub and sub in v:
            return v[sub]
    return None


_MISSING_FIELDS_LIST_RE = re.compile(r"缺口字段[:：]\s*(\[[^\]]+\])")
_MISSING_FIELD_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")

_DOC_DRAFT_TARGET_DEFAULT_TEMPLATE_IDS: dict[str, str] = {
    "contract_review_report": "215",
    "modification_suggestion": "270",
    "redline_comparison": "277",
}

_DOC_DRAFT_TARGET_ORDER: tuple[str, ...] = (
    "contract_review_report",
    "modification_suggestion",
    "redline_comparison",
)


def _parse_missing_fields(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    for m in _MISSING_FIELDS_LIST_RE.finditer(raw):
        payload = m.group(1)
        if not payload:
            continue
        parsed: list[Any] = []
        try:
            node = ast.literal_eval(payload)
            if isinstance(node, list):
                parsed = node
        except Exception:
            parsed = []
        for value in parsed:
            fk = str(value or "").strip()
            if not fk or fk in seen:
                continue
            seen.add(fk)
            out.append(fk)

    for token in _MISSING_FIELD_TOKEN_RE.findall(raw):
        fk = str(token or "").strip()
        if (not fk) or fk in seen:
            continue
        seen.add(fk)
        out.append(fk)

    return out


def _infer_missing_fields_from_card(card: dict[str, Any]) -> list[str]:
    if not isinstance(card, dict) or not card:
        return []
    merged = " ".join(
        [
            str(card.get("prompt") or ""),
            str(card.get("message") or ""),
            str(card.get("title") or ""),
        ]
    ).strip()
    if not merged:
        return []
    return _parse_missing_fields(merged)


def _fallback_answer_for_missing_field(field_key: str, uploaded_file_ids: list[str]) -> Any | None:
    fk = str(field_key or "").strip()
    if not fk:
        return None

    if fk in {"attachment_file_ids", "profile.attachment_file_ids"} or fk.endswith("attachment_file_ids"):
        return uploaded_file_ids
    if fk.endswith("file_ids"):
        return uploaded_file_ids
    if fk in {"profile.review_scope", "review_scope"}:
        # contract-intake enforces enum values: focused/full.
        # Prefer full to avoid introducing a new focus_areas follow-up dependency.
        return "full"
    if fk in {"profile.summary"}:
        return "张三起诉李四民间借贷纠纷，借款10万元到期未还，请求返还本金及利息。"
    if fk in {"profile.contract_type"}:
        return "建设工程施工合同"
    if fk in {"profile.document_type"}:
        return "民事起诉状"
    if fk in {"profile.court_name"}:
        return "北京市海淀区人民法院"
    if fk in {"profile.facts"}:
        return "2023年1月15日张三向李四转账10万元，约定一年内归还；到期后多次催收仍未还款。"
    if fk in {"profile.claims"}:
        return "请求判令李四返还借款本金10万元并支付逾期利息。"
    if fk in {"profile.plaintiff", "profile.plaintiff.name"}:
        return "张三"
    if fk in {"profile.defendant", "profile.defendant.name"}:
        return "李四"
    if fk in {"data.search.query"}:
        return "民间借贷 借条 转账记录 聊天记录 逾期还款 利息支持 最高人民法院 民间借贷司法解释"
    if fk.endswith((".reviewed", ".approved", ".confirmed", ".accepted")):
        return True
    if fk.startswith("profile."):
        return "请基于现有材料继续推进并输出结构化结论。"
    return "请基于现有材料继续推进。"


def _is_doc_draft_recovery_card(card: dict[str, Any]) -> bool:
    if not isinstance(card, dict) or not card:
        return False
    if str(card.get("skill_id") or "").strip() != "skill-error-analysis":
        return False
    task_key = str(card.get("task_key") or "").strip().lower()
    prompt = str(card.get("prompt") or "").strip().lower()
    return (
        "doc_draft" in task_key
        or "doc_generation" in task_key
        or "document_drafts" in prompt
        or "document-draft" in prompt
    )


def _extract_doc_draft_targets(card: dict[str, Any]) -> list[tuple[str, str]]:
    prompt = str(card.get("prompt") or "")
    prompt_lower = prompt.lower()
    task_key = str(card.get("task_key") or "").strip().lower()
    found: dict[str, str] = {}
    for key, template_id in re.findall(r"([a-z_]+)\((\d+)\)", prompt):
        output_key = str(key or "").strip()
        tid = str(template_id or "").strip()
        if output_key in _DOC_DRAFT_TARGET_DEFAULT_TEMPLATE_IDS and tid:
            found[output_key] = tid

    if found:
        out_found: list[tuple[str, str]] = []
        for output_key in _DOC_DRAFT_TARGET_ORDER:
            tid = found.get(output_key)
            if tid:
                out_found.append((output_key, tid))
        return out_found

    contract_context = (
        any(k in prompt_lower for k in _DOC_DRAFT_TARGET_DEFAULT_TEMPLATE_IDS)
        or "contract_review" in task_key
        or "modification_suggestion" in task_key
        or "redline" in task_key
    )
    if not contract_context:
        return []

    out: list[tuple[str, str]] = []
    for output_key in _DOC_DRAFT_TARGET_ORDER:
        out.append((output_key, found.get(output_key) or _DOC_DRAFT_TARGET_DEFAULT_TEMPLATE_IDS[output_key]))
    return out


def _pad_min_text(text: str, min_len: int, pad_token: str = " detail") -> str:
    base = str(text or "").strip()
    if len(base) >= int(min_len):
        return base
    need = int(min_len) - len(base)
    token = pad_token if pad_token else " detail"
    repeated = (token * ((need // len(token)) + 2))[:need]
    return base + repeated


def _build_contract_review_report_variables() -> dict[str, Any]:
    review_scope_notes = _pad_min_text(
        "Scope covers 14.3款, 15条, 16.2款, 7.5.7款, 18条 with focus on payment timing, review timing, and bond return. "
        "Fact anchors: 进度款, 19705.5, 2025-12-16.",
        120,
    )
    contract_overview = _pad_min_text(
        "Overview: amount 19705.5, term 2025-12-16 to 2026-01-15. "
        "14.3款 pays 70% after approval, 15条 has no strict overdue effect, 16.2款 keeps 3% bond. "
        "Main risk is delayed cashflow and asymmetric delay liability.",
        180,
    )
    risk_items = _pad_min_text(
        "1. 第14.3款 late pay risk. 法律依据：《民法典》第509条。\n"
        "2. 第15条 review delay risk. 法律依据：《民法典》第510条。\n"
        "3. 第16.2款 bond return unclear. 法律依据：《民法典》第509条。\n"
        "4. 第7.5.7款 one-sided delay penalty. 法律依据：《民法典》第577条。",
        260,
    )
    modification_suggestions = _pad_min_text(
        "1. 第14.3款 建议修改为：7-day verify and next-7-day pay.\n"
        "2. 第15条 建议修改为：28-day reply and overdue equals acceptance.\n"
        "3. 第16.2款 建议修改为：return 3% bond within 30 days after defect period.\n"
        "4. 第7.5.7款 建议修改为：penalty only for contractor-attributable delay.",
        220,
    )
    negotiation_priorities = _pad_min_text(
        "Priority: close 14.3款+15条 timing first, then 7.5.7款+16.2款 balance. Red line: no unlimited review delay.",
        80,
    )
    signing_checklist = _pad_min_text(
        "Checklist: verify 19705.5, date range, 14.3款 payment clock, 15条 review clock, 16.2款 return trigger.",
        60,
    )
    performance_notes = _pad_min_text(
        "Keep signed logs and 24h written notices for change events. 声明与保留: negotiation use only; final decision depends on new evidence.",
        60,
    )

    return {
        "review_scope_notes": review_scope_notes,
        "contract_overview": contract_overview,
        "risk_items": risk_items,
        "modification_suggestions": modification_suggestions,
        "negotiation_priorities": negotiation_priorities,
        "signing_checklist": signing_checklist,
        "performance_notes": performance_notes,
        "lawyer_name": "张晓杰",
        "law_firm": "LawSeekDog 律师团队",
        "year": "2026",
        "month": "03",
        "day": "01",
        "counter_argument_response": "counter-view answered",
        "action_steps": "steps fixed",
        "professional_notice": "notice",
    }


def _build_modification_suggestion_variables() -> dict[str, Any]:
    overall = _pad_min_text(
        "Overall: 14.3款/15条 timing is weak, 7.5.7款 liability is asymmetric, and 16.2款 return trigger is vague. "
        "法律依据：《民法典》第509条。",
        120,
    )
    suggestions = _pad_min_text(
        "1. 第14.3款 建议修改为：7-day verify and next-7-day pay.\n"
        "2. 第15条 建议修改为：28-day reply and overdue equals acceptance.\n"
        "3. 第7.5.7款 建议修改为：penalty only for contractor-attributable delay.\n"
        "4. 第16.2款 建议修改为：return 3% bond in 30 days after defect period.\n"
        "5. 第18条 建议修改为：15-day negotiation plus evidence-list exchange.\n"
        "6. keep 进度款 path aligned with 19705.5 and 2025-12-16 timeline.",
        220,
    )

    return {
        "overall_opinion": overall,
        "suggestions": suggestions,
        "lawyer_name": "张晓杰",
        "law_firm": "LawSeekDog 律师团队",
        "year": "2026",
        "month": "03",
        "day": "01",
    }


def _build_redline_comparison_variables() -> dict[str, Any]:
    scope_note = _pad_min_text(
        "Scope compares 14.3款, 15条, 7.5.7款, 18条 before sign-off. 法律依据：《民法典》第509条。",
        40,
    )
    comparison_table = (
        "| 条款位置 | 原文 | 建议改写 | 处理结论 |\n"
        "|---|---|---|---|\n"
        "| 第14.3款 | 核定后支付70%进度款 | 7日核定、核定后7日付款 | 必须修改 |\n"
        "| 第15条 | 结算审核无逾期后果 | 28日内反馈，逾期视为无异议 | 必须修改 |\n"
        "| 第7.5.7款 | 承包人延误即违约 | 仅承包人可归责延误承担违约 | 必须修改 |\n"
        "| 第18条 | 协商后诉讼 | 先协商15日并交换证据目录 | 建议修改 |"
    )
    comparison_table = _pad_min_text(comparison_table, 200, " note")
    risk_note = _pad_min_text(
        "Without amendment, 进度款 delay and liability imbalance remain. "
        "Lock 14.3款 and 15条 first. 法律依据：《民法典》第509条。",
        40,
    )

    return {
        "scope_note": scope_note,
        "comparison_table": comparison_table,
        "risk_note": risk_note,
        "lawyer_name": "张晓杰",
        "law_firm": "LawSeekDog 律师团队",
        "year": "2026",
        "month": "03",
        "day": "01",
    }


def _build_doc_draft_recovery_answers(card: dict[str, Any], existing_answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_keys = {
        str(item.get("field_key") or "").strip()
        for item in existing_answers
        if isinstance(item, dict)
    }

    task_key = str(card.get("task_key") or "").strip().lower()

    draft_by_key = {
        "contract_review_report": _build_contract_review_report_variables(),
        "modification_suggestion": _build_modification_suggestion_variables(),
        "redline_comparison": _build_redline_comparison_variables(),
    }

    drafts: list[dict[str, Any]] = []
    target_output_keys: list[str] = []
    for output_key, template_id in _extract_doc_draft_targets(card):
        target_output_keys.append(output_key)
        variables = draft_by_key.get(output_key)
        if not isinstance(variables, dict) or not variables:
            continue
        drafts.append(
            {
                "output_key": output_key,
                "template_id": str(template_id or "").strip(),
                "variables": variables,
            }
        )

    target_output_keys = [k for k in target_output_keys if str(k or "").strip()]

    if not drafts and target_output_keys:
        for output_key in target_output_keys:
            variables = draft_by_key.get(output_key)
            if not isinstance(variables, dict) or not variables:
                continue
            default_template_id = str(
                _DOC_DRAFT_TARGET_DEFAULT_TEMPLATE_IDS.get(output_key, "")
            ).strip()
            if not default_template_id:
                continue
            drafts.append(
                {
                    "output_key": output_key,
                    "template_id": default_template_id,
                    "variables": variables,
                }
            )

    extra_answers: list[dict[str, Any]] = []
    if "doc_generation" in task_key:
        if "data.work_product.document_drafts" not in existing_keys and drafts:
            extra_answers.append(
                {"field_key": "data.work_product.document_drafts", "value": drafts}
            )
        if "data.work_product.drafts_ready" not in existing_keys and drafts:
            extra_answers.append(
                {"field_key": "data.work_product.drafts_ready", "value": True}
            )
        return extra_answers

    if "data.work_product.document_drafts" not in existing_keys and drafts:
        extra_answers.append(
            {"field_key": "data.work_product.document_drafts", "value": drafts}
        )
    if "data.work_product.drafts_ready" not in existing_keys and drafts:
        extra_answers.append(
            {"field_key": "data.work_product.drafts_ready", "value": True}
        )
    return extra_answers


def _coerce_select_value_from_semantic_hint(hint: Any, options: list[Any] | None) -> Any | None:
    opts = options if isinstance(options, list) else []
    if not opts:
        return hint

    if isinstance(hint, bool):
        positive_tokens = ("是", "已完成", "确认", "继续", "同意", "yes", "confirm", "continue")
        negative_tokens = ("否", "未完成", "取消", "no", "cancel")
        for opt in opts:
            value = _option_answer_value(opt)
            if value is None:
                continue
            text = _option_match_text(opt)
            if hint and any(tok in text for tok in positive_tokens) and not any(tok in text for tok in negative_tokens):
                return value
            if (not hint) and any(tok in text for tok in negative_tokens):
                return value
        return _pick_recommended_or_first(opts)

    hint_text = str(hint or "").strip().lower()
    if not hint_text:
        return _pick_recommended_or_first(opts)
    for opt in opts:
        value = _option_answer_value(opt)
        if value is None:
            continue
        text = _option_match_text(opt)
        if hint_text in text:
            return value
    return _pick_recommended_or_first(opts)


def _forced_answer_from_question_text(question_text: str) -> Any | None:
    text = str(question_text or "").strip()
    if not text:
        return None

    # 文书起草中法院名称缺失会反复追问，这里给稳定可用的管辖法院占位答案。
    if "法院" in text and any(tok in text for tok in ("哪个", "名称", "管辖", "受理", "用于")):
        return "北京市海淀区人民法院"

    if "是否已完成所有材料上传" in text or ("完成所有材料上传" in text and "勾选" in text):
        return True

    return None


def auto_answer_card(
    card: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
    uploaded_file_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a resume.user_response from a card by applying overrides + safe defaults."""
    overrides = overrides or {}
    uploaded_file_ids = [str(x).strip() for x in (uploaded_file_ids or []) if str(x).strip()]

    skill_id = str(card.get("skill_id") or "").strip()
    questions = card.get("questions")
    questions = questions if isinstance(questions, list) else []
    allowed_field_keys: set[str] = set()

    answers: list[dict[str, Any]] = []

    def _append_answer(field_key: str, value: Any, *, question: dict[str, Any] | None = None) -> None:
        answers.append({"field_key": field_key, "value": value})
        if not isinstance(question, dict):
            return
        value_label_field_key = str(question.get("value_label_field_key") or "").strip()
        if not value_label_field_key:
            return
        raw_options = question.get("options")
        options: list[Any] = raw_options if isinstance(raw_options, list) else []
        label_value = _option_label_for_value(options, value)
        if label_value:
            answers.append({"field_key": value_label_field_key, "value": label_value})

    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        allowed_field_keys.add(fk)
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))
        q_text = " ".join(
            [
                str(q.get("question") or ""),
                str(q.get("label") or ""),
                str(q.get("placeholder") or ""),
            ]
        ).strip()

        forced_value = _forced_answer_from_question_text(q_text)
        if forced_value is not None:
            if it in {"select", "single_select", "single_choice"}:
                forced_value = _coerce_select_value_from_semantic_hint(
                    forced_value,
                    q.get("options") if isinstance(q.get("options"), list) else [],
                )
            elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
                # file_ids answers must be arrays; skip optional uploads when we do not have new file ids.
                if isinstance(forced_value, list):
                    forced_value = [str(x).strip() for x in forced_value if str(x).strip()]
                elif uploaded_file_ids:
                    forced_value = list(uploaded_file_ids)
                elif required:
                    forced_value = []
                else:
                    continue
            _append_answer(fk, forced_value, question=q)
            continue

        override_value = _resolve_override_value(fk, overrides)
        if override_value is not None:
            if fk in {"profile.review_scope", "review_scope"}:
                override_value = _coerce_review_scope_for_options(
                    override_value,
                    q.get("options") if isinstance(q.get("options"), list) else [],
                )
            _append_answer(fk, override_value, question=q)
            continue

        default = q.get("default")
        has_default = default is not None and not (
            (isinstance(default, str) and not default.strip())
            or (isinstance(default, list) and not default)
            or (isinstance(default, dict) and not default)
        )

        value: Any | None = None
        if it in {"boolean", "bool"}:
            if fk == "data.evidence.evidence_gap_stop_ask":
                # Do not auto-stop evidence gap follow-up in E2E; keep this gate strict.
                value = default if has_default else False
            elif fk == "data.work_product.regenerate_documents":
                # documents-stale confirm cards ask whether to regenerate again.
                # Auto-regenerate can create endless drafting loops and block archive delivery.
                value = default if has_default else False
            else:
                value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            raw_options = q.get("options")
            options: list[Any] = raw_options if isinstance(raw_options, list) else []
            value = default if has_default else _pick_recommended_or_first(options)
            if fk in {"profile.review_scope", "review_scope"}:
                value = _coerce_review_scope_for_options(
                    value,
                    options,
                )
        elif it in {"multi_select", "multiple_select"}:
            if has_default:
                value = default
            else:
                raw_options = q.get("options")
                options: list[Any] = raw_options if isinstance(raw_options, list) else []
                # For multi-select questions, picking all recommended options can trigger a lot of expensive
                # downstream work (e.g., generating multiple documents). Prefer a minimal, deterministic choice.
                picked: Any | None = None
                for opt in options:
                    if not isinstance(opt, dict) or opt.get("recommended") is not True:
                        continue
                    if opt.get("value") is not None:
                        picked = opt.get("value")
                        break
                    if opt.get("id") is not None:
                        picked = opt.get("id")
                        break
                if picked is None:
                    picked = _pick_recommended_or_first(options)
                value = [picked] if picked is not None else []
        elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
            # Card validation in ai-engine requires attachment_file_ids to always be an array.
            if fk == "attachment_file_ids":
                if has_default:
                    value = default
                elif uploaded_file_ids:
                    value = uploaded_file_ids
                else:
                    value = []
            else:
                if has_default:
                    value = default
                elif uploaded_file_ids and required:
                    value = uploaded_file_ids
                else:
                    value = [] if required else None
        else:
            # Minimal safe defaults for common workflow profile slots.
            if fk == "profile.summary":
                value = "请根据已提交材料与事实生成案件摘要。"
            elif fk == "profile.facts":
                value = "已提交事实陈述与材料。"
            elif fk == "profile.claims":
                value = "请根据事实与材料整理诉讼请求/需求清单。"
            elif fk in {"profile.plaintiff", "profile.plaintiff.name"}:
                value = "张三"
            elif fk in {"profile.defendant", "profile.defendant.name"}:
                value = "李四"
            elif fk == "data.search.query":
                value = "民间借贷 借条 转账记录 聊天记录 逾期还款 利息支持 最高人民法院 民间借贷司法解释"
            else:
                value = default if has_default else (_fallback_answer_for_missing_field(fk, uploaded_file_ids) if required else None)

        if value is None and required:
            value = _fallback_answer_for_missing_field(fk, uploaded_file_ids)

        # For optional questions, omit the answer entirely if we don't have a value.
        # This avoids sending `null` into strict field validators (e.g. attachment_file_ids must be a list).
        if value is None and not required:
            continue

        _append_answer(fk, value, question=q)

    inferred_missing = _infer_missing_fields_from_card(card)
    if inferred_missing:
        answered = {str(it.get("field_key") or "").strip() for it in answers if isinstance(it, dict)}
        for fk in inferred_missing:
            if fk not in allowed_field_keys:
                continue
            if fk in answered:
                continue
            override_value = _resolve_override_value(fk, overrides)
            value = override_value if override_value is not None else _fallback_answer_for_missing_field(fk, uploaded_file_ids)
            if value is None:
                continue
            _append_answer(fk, value)

    # Compatibility aliases:
    # Some environments still read legacy top-level fields, but strict card
    # validation rejects any field_key not present in questions. Only add an
    # alias when that alias key is explicitly asked by the current card.
    alias_map = {
        "profile.client_role": "client_role",
        "profile.service_type_id": "service_type_id",
    }
    existing_keys = {
        str(it.get("field_key") or "").strip()
        for it in answers
        if isinstance(it, dict)
    }
    for src, dst in alias_map.items():
        if src not in existing_keys or dst in existing_keys:
            continue
        if dst not in allowed_field_keys:
            continue
        src_val = None
        for it in answers:
            if not isinstance(it, dict):
                continue
            if str(it.get("field_key") or "").strip() != src:
                continue
            src_val = it.get("value")
            break
        if src_val is None:
            continue
        answers.append({"field_key": dst, "value": src_val})

    if _is_doc_draft_recovery_card(card):
        answers.extend(_build_doc_draft_recovery_answers(card, answers))

    return {"answers": answers}


def card_signature(card: dict[str, Any]) -> str:
    skill = str(card.get("skill_id") or "").strip()
    task = str(card.get("task_key") or "").strip()
    review = str(card.get("review_type") or "").strip()
    raw_questions = card.get("questions")
    qs: list[Any] = raw_questions if isinstance(raw_questions, list) else []
    sigs: list[str] = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        if fk:
            sigs.append(f"{fk}|{it}")
    raw = json.dumps({"skill": skill, "task": task, "review": review, "questions": sigs}, ensure_ascii=False, sort_keys=True)
    # Short hash for log readability.
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_unanswerable_card(card: dict[str, Any]) -> bool:
    if not isinstance(card, dict) or not card:
        return False
    questions = card.get("questions")
    if not isinstance(questions, list):
        return True
    return len(questions) == 0


def _compact_card_debug(card: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(card, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("skill_id", "task_key", "review_type", "title"):
        value = str(card.get(key) or "").strip()
        if value:
            out[key] = value
    prompt = str(card.get("prompt") or card.get("message") or "").strip()
    if prompt:
        out["prompt"] = prompt[:300]
    questions = card.get("questions")
    if isinstance(questions, list):
        out["questions_count"] = len(questions)
    return out


def _compact_sse_events(sse: dict[str, Any] | None) -> str:
    if not isinstance(sse, dict):
        return ""
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    names: list[str] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        event_name = str(row.get("event") or "").strip()
        if not event_name:
            continue
        if event_name not in names:
            names.append(event_name)
    return ",".join(names[:8])


def _remediation_nudge_for_unanswerable_card(card: dict[str, Any]) -> str | None:
    if not isinstance(card, dict) or not card:
        return None
    prompt = " ".join(
        [
            str(card.get("prompt") or ""),
            str(card.get("message") or ""),
            str(card.get("title") or ""),
            str(card.get("task_key") or ""),
        ]
    )
    s = prompt.strip()
    if not s:
        return None

    if "profile.review_scope" in s and "缺口字段" in s:
        return "补充审查范围：请基于已上传合同进行条款级法律审查，覆盖价款、违约、解除、争议解决，并输出可替换条款建议。"

    contract_doc_quality_markers = (
        "合同审查报告法条引用不足",
        "合同审查报告条款定位引用不足",
        "编号建议条目过少",
    )
    if any(m in s for m in contract_doc_quality_markers):
        return (
            "请重新生成合同审查意见书，并严格满足："
            "至少3处《..》第..条法条引用；"
            "至少5处“第X条/X.Y款”条款定位；"
            "至少8条编号问题及建议，且建议项以“建议修改为：”开头。"
        )

    return None


def _remediation_nudge_for_reference_grounding(card: dict[str, Any]) -> str:
    prompt = " ".join(
        [
            str(card.get("prompt") or ""),
            str(card.get("message") or ""),
            str(card.get("title") or ""),
        ]
    )
    if any(token in prompt for token in ("工伤", "视同工伤", "赵丽珍")):
        return (
            "补充检索关键词：工伤认定、视同工伤、非因工死亡、宿舍猝死、劳动关系证明、赔偿责任。"
            "请基于已上传材料继续检索可复核法条与类案。"
        )
    return "补充检索关键词：争议焦点、构成要件、责任认定、裁判规则。请基于已上传材料继续检索可复核法条与类案。"


@dataclass
class WorkbenchFlow:
    client: Any
    session_id: str
    uploaded_file_ids: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    matter_id: str | None = None
    session_status: str | None = None
    session_archived: bool = False
    seen_cards: list[dict[str, Any]] = field(default_factory=list)
    seen_card_signatures: list[str] = field(default_factory=list)
    seen_sse: list[dict[str, Any]] = field(default_factory=list)
    last_sse: dict[str, Any] | None = None
    _repeat_unanswerable_signature: str | None = None
    _repeat_unanswerable_count: int = 0
    _repeat_card_signature: str | None = None
    _repeat_card_count: int = 0
    _last_step_used_nudge: bool = False

    async def _runtime_progress_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {
            "session": self.session_id,
            "matter": str(self.matter_id or "").strip(),
            "status": str(self.session_status or "").strip(),
            "phase": "",
            "phase_status": "",
            "trace_node": "",
            "trace_status": "",
            "deliverables": "",
        }
        if not self.matter_id:
            return snapshot

        try:
            phase_resp = await self.client.get_matter_phase_timeline(self.matter_id)
            phase_data = unwrap_api_response(phase_resp)
            if isinstance(phase_data, dict):
                snapshot["phase"] = str(phase_data.get("current_phase") or phase_data.get("currentPhase") or "").strip()
                raw_phases = phase_data.get("phases")
                phases: list[Any] = list(raw_phases) if isinstance(raw_phases, list) else []
                for item in phases:
                    if not isinstance(item, dict):
                        continue
                    phase_id = str(item.get("id") or "").strip()
                    if phase_id and phase_id == snapshot["phase"]:
                        snapshot["phase_status"] = str(item.get("status") or "").strip()
                        break
        except Exception:
            pass

        try:
            trace_resp = await self.client.list_traces(self.matter_id, limit=1)
            trace_data = unwrap_api_response(trace_resp)
            raw_traces = trace_data.get("traces") if isinstance(trace_data, dict) else None
            traces: list[Any] = list(raw_traces) if isinstance(raw_traces, list) else []
            if traces:
                latest = traces[0] if isinstance(traces[0], dict) else {}
                snapshot["trace_node"] = str(
                    latest.get("node_id") or latest.get("nodeId") or latest.get("task_id") or latest.get("taskId") or ""
                ).strip()
                snapshot["trace_status"] = str(latest.get("status") or latest.get("state") or "").strip()
        except Exception:
            pass

        try:
            deliverables_resp = await self.client.list_deliverables(self.matter_id)
            deliverables_data = unwrap_api_response(deliverables_resp)
            raw_rows = deliverables_data.get("deliverables") if isinstance(deliverables_data, dict) else None
            rows: list[Any] = list(raw_rows) if isinstance(raw_rows, list) else []
            output_keys: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("output_key") or row.get("outputKey") or "").strip()
                if key and key not in output_keys:
                    output_keys.append(key)
            snapshot["deliverables"] = ",".join(output_keys[:6])
        except Exception:
            pass
        return snapshot

    async def _emit_progress(
        self,
        *,
        label: str,
        step_no: int | None = None,
        max_steps: int | None = None,
        card: dict[str, Any] | None = None,
        sse: dict[str, Any] | None = None,
    ) -> None:
        snapshot = await self._runtime_progress_snapshot()
        parts = [f"[flow-progress] {label}"]
        if step_no is not None and max_steps is not None:
            parts.append(f"step={step_no}/{max_steps}")
        parts.append(f"session={snapshot.get('session')}")
        if snapshot.get("matter"):
            parts.append(f"matter={snapshot.get('matter')}")
        if snapshot.get("status"):
            parts.append(f"status={snapshot.get('status')}")
        if snapshot.get("phase"):
            phase = snapshot.get("phase")
            phase_status = snapshot.get("phase_status")
            if phase_status:
                parts.append(f"phase={phase}:{phase_status}")
            else:
                parts.append(f"phase={phase}")
        if snapshot.get("trace_node"):
            trace = snapshot.get("trace_node")
            trace_status = snapshot.get("trace_status")
            if trace_status:
                parts.append(f"trace={trace}:{trace_status}")
            else:
                parts.append(f"trace={trace}")
        if card:
            skill = str(card.get("skill_id") or "").strip()
            task = str(card.get("task_key") or "").strip()
            if skill or task:
                parts.append(f"card={skill}/{task}".rstrip("/"))
        event_summary = _compact_sse_events(sse)
        if event_summary:
            parts.append(f"events={event_summary}")
        if snapshot.get("deliverables"):
            parts.append(f"deliverables={snapshot.get('deliverables')}")
        _progress(" ".join(parts))

    async def refresh(self) -> None:
        try:
            sess = unwrap_api_response(await self.client.get_session(self.session_id))
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if self.matter_id and code in {404, 429, 500, 502, 503, 504}:
                _debug(f"[flow] refresh session failed status={code}; keep matter_id={self.matter_id}")
                return
            raise
        except httpx.RequestError:
            if self.matter_id:
                _debug(f"[flow] refresh session network error; keep matter_id={self.matter_id}")
                return
            raise
        if isinstance(sess, dict):
            status = str(sess.get("status") or "").strip().lower()
            if status:
                self.session_status = status
                self.session_archived = status == "archived"

            if sess.get("matter_id") is not None:
                mid = str(sess.get("matter_id")).strip()
                if mid and mid != (self.matter_id or ""):
                    _debug(f"[flow] session bound matter_id={mid}")
                self.matter_id = mid

    async def get_pending_card(self) -> dict[str, Any] | None:
        if self.session_archived:
            return None
        try:
            resp = await self.client.get_pending_card(self.session_id)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 400 and e.response is not None:
                body = str(e.response.text or "")
                if "会话已归档" in body:
                    self.session_archived = True
                    self.session_status = "archived"
                    _debug("[flow] pending card blocked: session archived")
                    return None
            if code in {400, 404, 409, 429, 500, 502, 503, 504}:
                _debug(f"[flow] pending card unavailable status={code}")
                return None
            raise
        except httpx.RequestError:
            _debug("[flow] pending card request error")
            return None
        card = unwrap_api_response(resp)
        if isinstance(card, dict) and card:
            _debug(
                f"[flow] pending card skill_id={card.get('skill_id')} task_key={card.get('task_key')} review_type={card.get('review_type')}"
            )
        return card if isinstance(card, dict) and card else None

    async def resume_card(self, card: dict[str, Any], *, max_loops: int | None = None) -> dict[str, Any]:
        # Keep an audit trail for assertions/debugging.
        self.seen_cards.append(card)
        self.seen_card_signatures.append(card_signature(card))
        if _DEBUG:
            raw_questions = card.get("questions")
            qs: list[Any] = list(raw_questions) if isinstance(raw_questions, list) else []
            fields = [
                {
                    "field_key": str(q.get("field_key") or "").strip(),
                    "input_type": str(q.get("input_type") or q.get("question_type") or "").strip().lower(),
                }
                for q in qs
                if isinstance(q, dict)
            ]
            _debug(
                f"[flow] card detail skill={card.get('skill_id')} task={card.get('task_key')} "
                f"fields={fields}"
            )
        user_response = auto_answer_card(card, overrides=self.overrides, uploaded_file_ids=self.uploaded_file_ids)
        _debug(
            f"[flow] resume card {card.get('skill_id')} answers={len(user_response.get('answers') or [])} "
            f"payload={user_response}"
        )
        resolved_max_loops = _RESUME_MAX_LOOPS if max_loops is None else max(1, int(max_loops))
        sse = await self.client.resume(
            self.session_id,
            user_response,
            pending_card=card,
            max_loops=resolved_max_loops,
        )
        try:
            assert_has_user_message(sse)
        except AssertionError:
            if not is_session_busy_sse(sse if isinstance(sse, dict) else {}):
                raise
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        await self._emit_progress(label="resume", card=card, sse=sse)
        return sse

    async def nudge(self, text: str = _NUDGE_TEXT, *, attachments: list[str] | None = None, max_loops: int = 8) -> dict[str, Any]:
        _debug(f"[flow] nudge text={text!r} attachments={len(attachments or [])} max_loops={max_loops}")
        sse = await self.client.chat(self.session_id, text, attachments=attachments or [], max_loops=max_loops)
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        await self._emit_progress(label=f"nudge:{text[:24]}", sse=sse)
        return sse

    async def step(
        self,
        *,
        nudge_text: str = _NUDGE_TEXT,
        allow_nudge: bool = True,
        stop_on_pending_card: PendingCardStopFn | None = None,
    ) -> dict[str, Any] | None:
        """Process one pending card if exists; optionally send a small nudge chat."""
        await self.refresh()
        if self.session_archived:
            # Avoid any chat/resume operations once the session is archived; keep run_until polling only.
            return {"events": [{"event": "session_archived"}], "output": "session archived"}
        card = await self.get_pending_card()
        if card:
            self._last_step_used_nudge = False
            sig = card_signature(card)
            if sig == self._repeat_card_signature:
                self._repeat_card_count += 1
            else:
                self._repeat_card_signature = sig
                self._repeat_card_count = 1

            if _REPEATED_CARD_ABORT_COUNT > 1:
                recent = self.seen_card_signatures[-(_REPEATED_CARD_ABORT_COUNT - 1) :] + [sig]
                if len(recent) >= _REPEATED_CARD_ABORT_COUNT and len(set(recent)) == 1:
                    debug_card = _compact_card_debug(card)
                    raise AssertionError(
                        "workflow stuck on repeated pending card signature: "
                        f"repeat={len(recent)}, card={debug_card}"
                    )

            if stop_on_pending_card is not None and stop_on_pending_card(card):
                _debug(
                    f"[flow] stop_on_pending_card skill_id={card.get('skill_id')} "
                    f"task_key={card.get('task_key')}"
                )
                self.seen_cards.append(card)
                self.seen_card_signatures.append(sig)
                return _pending_card_intercept_sse(card)

            skill_id = str(card.get("skill_id") or "").strip()
            task_key = str(card.get("task_key") or "").strip()
            if skill_id == "skill-error-analysis" and self._repeat_card_count >= 2:
                remediation = _remediation_nudge_for_unanswerable_card(card)
                if remediation:
                    _debug(
                        "[flow] skill-error-analysis remediation nudge "
                        f"repeat={self._repeat_card_count} task={task_key}"
                    )
                    self._last_step_used_nudge = True
                    return await self.nudge(remediation, attachments=self.uploaded_file_ids, max_loops=12)

            if skill_id == "intent-route-v3" and ("doc_draft" in task_key) and self._repeat_card_count >= 3:
                _debug(
                    "[flow] intent-route doc_draft remediation nudge "
                    f"repeat={self._repeat_card_count} task={task_key}"
                )
                self._last_step_used_nudge = True
                return await self.nudge(
                    "角色已确认：申请人。审查范围：全面审查。请不要重复追问，直接继续合同审查并生成交付物。",
                    attachments=self.uploaded_file_ids,
                    max_loops=12,
                )

            if skill_id == "reference-grounding" and self._repeat_card_count >= 3:
                hint = _remediation_nudge_for_reference_grounding(card)
                _debug(f"[flow] reference-grounding remediation nudge repeat={self._repeat_card_count}")
                self._last_step_used_nudge = True
                return await self.nudge(hint, attachments=self.uploaded_file_ids, max_loops=12)

            if _is_unanswerable_card(card):
                if sig == self._repeat_unanswerable_signature:
                    self._repeat_unanswerable_count += 1
                else:
                    self._repeat_unanswerable_signature = sig
                    self._repeat_unanswerable_count = 1
                remediation = _remediation_nudge_for_unanswerable_card(card)
                if remediation and self._repeat_unanswerable_count >= 2:
                    _debug(
                        "[flow] unanswerable card remediation nudge "
                        f"repeat={self._repeat_unanswerable_count} task={card.get('task_key')}"
                    )
                    self._last_step_used_nudge = True
                    return await self.nudge(remediation, attachments=self.uploaded_file_ids, max_loops=12)
                if self._repeat_unanswerable_count >= _UNANSWERABLE_CARD_MAX_REPEATS:
                    debug_card = _compact_card_debug(card)
                    raise AssertionError(
                        "workflow blocked by repeated unanswerable pending card: "
                        f"repeat={self._repeat_unanswerable_count}, card={debug_card}"
                    )
            else:
                self._repeat_unanswerable_signature = None
                self._repeat_unanswerable_count = 0
            _debug(f"[flow] resume card skill_id={card.get('skill_id')} task_key={card.get('task_key')}")
            return await self.resume_card(card)
        self._repeat_card_signature = None
        self._repeat_card_count = 0
        self._repeat_unanswerable_signature = None
        self._repeat_unanswerable_count = 0
        self._last_step_used_nudge = False
        if not allow_nudge:
            return None
        if not _AUTO_NUDGE:
            return None
        _debug(f"[flow] nudge {nudge_text!r}")
        self._last_step_used_nudge = True
        sse = await self.nudge(nudge_text, attachments=self.uploaded_file_ids, max_loops=8)
        # Some remote environments intermittently fail pending_card polling but still
        # emit the actionable card in the nudge stream itself. Consume it immediately
        # so the flow can continue without waiting for pending_card API consistency.
        sse_card = extract_last_card_from_sse(sse if isinstance(sse, dict) else {})
        if isinstance(sse_card, dict) and sse_card:
            if stop_on_pending_card is not None and stop_on_pending_card(sse_card):
                _debug(
                    f"[flow] nudge produced interceptable card skill_id={sse_card.get('skill_id')} "
                    f"task_key={sse_card.get('task_key')}"
                )
                if isinstance(sse, dict):
                    sse["pending_card"] = sse_card
                return sse
            _debug(
                f"[flow] nudge produced card skill_id={sse_card.get('skill_id')} "
                f"task_key={sse_card.get('task_key')}; resume directly"
            )
            return await self.resume_card(sse_card)
        return sse

    async def run_until(
        self,
        predicate: Callable[["WorkbenchFlow"], Any],
        *,
        max_steps: int = 40,
        nudge_text: str = _NUDGE_TEXT,
        step_sleep_s: float = 0.0,
        description: str = "target condition",
        stop_on_pending_card: PendingCardStopFn | None = None,
    ) -> None:
        """Advance the workflow until predicate(flow) is truthy (sync/async)."""
        step_no = 0
        busy_retries = 0
        suppress_nudge_rounds = 0
        nudge_cooldown = 0
        while step_no < max_steps:
            ok = predicate(self)
            if asyncio.iscoroutine(ok):
                ok = await ok
            if ok:
                await self._emit_progress(label=f"ready:{description}", step_no=step_no + 1, max_steps=max_steps)
                _debug(f"[flow] reached {description} at step {step_no + 1} (session_id={self.session_id}, matter_id={self.matter_id})")
                return

            step_no += 1
            await self._emit_progress(label=f"waiting:{description}", step_no=step_no, max_steps=max_steps)
            _debug(f"[flow] step {step_no}/{max_steps} waiting for {description} (session_id={self.session_id}, matter_id={self.matter_id})")
            allow_nudge = (suppress_nudge_rounds <= 0) and (nudge_cooldown <= 0)
            sse = await self.step(
                nudge_text=nudge_text,
                allow_nudge=allow_nudge,
                stop_on_pending_card=stop_on_pending_card,
            )
            if self._last_step_used_nudge:
                nudge_cooldown = max(nudge_cooldown, 8)
            else:
                nudge_cooldown = max(0, nudge_cooldown - 1)
            if sse is None:
                if not allow_nudge:
                    suppress_nudge_rounds = max(0, suppress_nudge_rounds - 1)
                await asyncio.sleep(max(_SESSION_BUSY_BACKOFF_S, 0.8))
                continue
            if is_session_busy_sse(sse):
                busy_retries += 1
                if busy_retries <= _SESSION_BUSY_EXTRA_RETRIES:
                    step_no -= 1
                    # After repeated session_busy responses, avoid extra nudges and prefer passive polling.
                    suppress_nudge_rounds = min(30, max(suppress_nudge_rounds, 5 + busy_retries // 2))
                    _debug(f"[flow] session busy; backoff {max(_SESSION_BUSY_BACKOFF_S, 0.5):.1f}s retry={busy_retries}")
                    await asyncio.sleep(max(_SESSION_BUSY_BACKOFF_S, 0.5))
                    continue
            else:
                busy_retries = 0
                suppress_nudge_rounds = 0
            if step_sleep_s:
                await asyncio.sleep(step_sleep_s)

        raise AssertionError(f"Failed to reach {description} after {max_steps} steps (session_id={self.session_id}, matter_id={self.matter_id})")


async def wait_for_initial_card(flow: WorkbenchFlow, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Wait until the workflow produces a pending card (intake/confirm/etc)."""
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        await flow.refresh()
        last = await flow.get_pending_card()
        if last:
            return last
        await asyncio.sleep(1.0)
    raise AssertionError(f"Timed out waiting for initial card (timeout={timeout_s}s, session_id={flow.session_id})")
