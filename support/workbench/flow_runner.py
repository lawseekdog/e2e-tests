"""Session-driven workflow runner for lawyer workbench E2E.

This drives the real chain:
gateway -> consultations-service -> ai-engine (LangGraph) -> matter-service/files-service/templates/knowledge/memory.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .utils import trim, unwrap_api_response
from .sse import assert_has_user_message

BlockerStopFn = Callable[[dict[str, Any]], bool]
ProgressObserver = Callable[[dict[str, Any]], Any]

_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}
_PROGRESS = str(os.getenv("E2E_FLOW_PROGRESS", "1") or "").strip().lower() in {"1", "true", "yes"}
_STRICT_CARD_DRIVEN_DEFAULT = str(os.getenv("E2E_STRICT_CARD_DRIVEN", "1") or "").strip().lower() in {"1", "true", "yes"}


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
_CARD_RESUME_SETTLE_TIMEOUT_S = float(os.getenv("E2E_CARD_RESUME_SETTLE_TIMEOUT_S", "45") or 45)


def _debug(msg: str) -> None:
    if _DEBUG:
        print(msg, flush=True)


def _progress(msg: str) -> None:
    if _PROGRESS:
        print(msg, flush=True)


def extract_last_blocker_from_sse(sse: dict[str, Any]) -> dict[str, Any] | None:
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    for idx in range(len(events) - 1, -1, -1):
        it = events[idx]
        if not isinstance(it, dict):
            continue
        if it.get("event") not in {"awaiting_review", "blocked"}:
            continue
        data = it.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def extract_last_card_from_sse(sse: dict[str, Any]) -> dict[str, Any] | None:
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    for idx in range(len(events) - 1, -1, -1):
        it = events[idx]
        if not isinstance(it, dict):
            continue
        if str(it.get("event") or "").strip() != "card":
            continue
        data = it.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _compact_blocker(value: Any) -> dict[str, str]:
    blocker = _as_dict(value)
    if not blocker:
        return {}
    out: dict[str, str] = {}
    for key in (
        "type",
        "interruption_id",
        "interruption_key",
        "reason_kind",
        "reason_code",
        "title",
        "summary",
        "prompt",
        "product_type",
        "status",
    ):
        token = str(blocker.get(key) or "").strip()
        if token:
            out[key] = token
    return out


def _resolve_current_phase_row(phases_value: Any) -> dict[str, Any]:
    phases = _as_list(phases_value)
    current_rows = [row for row in phases if isinstance(row, dict) and row.get("current") is True]
    if len(current_rows) != 1:
        raise AssertionError(f"workflow phases must contain exactly one current=true phase, got {len(current_rows)}")
    current_row = current_rows[0]
    phase_id = str(current_row.get("phase_id") or current_row.get("id") or "").strip()
    if not phase_id:
        raise AssertionError("workflow current phase is missing phase_id/id")
    return current_row


def _blocker_label(blocker: dict[str, Any] | None) -> str:
    row = blocker if isinstance(blocker, dict) else {}
    kind = str(row.get("type") or "").strip()
    ident = (
        str(
            row.get("interruption_id")
            or row.get("interruption_key")
            or row.get("reason_code")
            or ""
        ).strip()
    )
    if kind and ident:
        return f"{kind}:{ident}"
    return (
        str(row.get("summary") or "").strip()
        or str(row.get("title") or "").strip()
        or kind
    )


def _extract_runtime_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    direct = _as_dict(snapshot.get("workbench_runtime"))
    if direct:
        return direct
    analysis_state = _as_dict(snapshot.get("analysis_state"))
    nested = _as_dict(analysis_state.get("workbench_runtime"))
    if nested:
        return nested
    if analysis_state:
        return analysis_state
    return {}


def _snapshot_pending_task_count(snapshot: dict[str, Any] | None) -> int | None:
    if not isinstance(snapshot, dict):
        return None

    candidates: list[Any] = [
        _as_dict(snapshot.get("matter")).get("pending_task_count"),
        snapshot.get("pending_task_count"),
    ]
    runtime = _extract_runtime_snapshot(snapshot)
    if runtime:
        candidates.extend(
            [
                runtime.get("pending_task_count"),
            ]
        )

    for raw in candidates:
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        text = str(raw or "").strip()
        if text.isdigit():
            return int(text)
    return None


def _snapshot_awaiting_user_input(snapshot: dict[str, Any] | None) -> bool | None:
    runtime = _extract_runtime_snapshot(snapshot)
    if not runtime:
        return None

    direct = runtime.get("awaiting_user_input")
    if isinstance(direct, bool):
        return direct

    routing = _as_dict(runtime.get("routing"))
    nested = routing.get("awaiting_user_input")
    if isinstance(nested, bool):
        return nested
    return None


def _is_goal_completion_blocker(blocker: dict[str, Any]) -> bool:
    interruption_key = str(blocker.get("interruption_key") or "").strip().lower()
    reason_code = str(blocker.get("reason_code") or "").strip().lower()
    product_type = str(blocker.get("product_type") or "").strip().lower()
    return (
        interruption_key == "goal_completion"
        or reason_code == "goal_completion"
        or product_type == "goal_completion"
    )


def _blocker_intercept_sse(blocker: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [{"event": str(blocker.get("type") or "awaiting_review"), "data": blocker}],
        "current_blocker": blocker,
        "output": "blocker intercepted",
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


def _is_effective_resume_sse(sse: dict[str, Any] | None) -> bool:
    if not isinstance(sse, dict):
        return False
    raw_events = sse.get("events")
    events: list[Any] = list(raw_events) if isinstance(raw_events, list) else []
    for it in events:
        if not isinstance(it, dict):
            continue
        event_name = str(it.get("event") or "").strip()
        if event_name in {
            "resume_submitted",
            "progress",
            "task_start",
            "awaiting_review",
            "blocked",
            "result",
            "error",
            "end",
            "complete",
        }:
            return True
    return False


def _is_skill_error_confirm_card(blocker: dict[str, Any] | None) -> bool:
    if not isinstance(blocker, dict):
        return False
    return (
        str(blocker.get("reason_code") or "").strip() == "skill_error_analysis"
        and str(blocker.get("reason_kind") or "").strip().lower() == "human_confirmation"
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
    aliases = {
        "quick": {"quick", "快速审查", "快速筛查", "quick_review"},
        "risk": {"risk", "风险审查", "风险筛查"},
        "redline": {"redline", "红线审查", "red_line"},
        "full": {"full", "全面审查", "全面", "all"},
    }
    for normalized, tokens in aliases.items():
        if s in tokens:
            return normalized
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


def _pick_all_recommended_values(options: list[Any]) -> list[Any]:
    picked: list[Any] = []
    seen: set[str] = set()
    for opt in options if isinstance(options, list) else []:
        if not isinstance(opt, dict) or opt.get("recommended") is not True:
            continue
        value = _option_answer_value(opt)
        token = str(value).strip()
        if value is None or not token or token in seen:
            continue
        seen.add(token)
        picked.append(value)
    return picked


def _pick_contract_review_clause_values(options: list[Any]) -> list[Any]:
    picked = _pick_all_recommended_values(options)
    if picked:
        return picked

    risk_tokens = ("high", "critical", "medium", "高风险", "重大", "中风险")
    out: list[Any] = []
    seen: set[str] = set()
    for opt in options if isinstance(options, list) else []:
        value = _option_answer_value(opt)
        token = str(value).strip()
        text = _option_match_text(opt)
        if value is None or not token or token in seen:
            continue
        if any(risk_token in text for risk_token in risk_tokens):
            seen.add(token)
            out.append(value)
    if out:
        return out

    fallback = _pick_recommended_or_first(options)
    return [fallback] if fallback is not None else []


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

    review_scope_tokens = {
        "quick": ("quick", "快速"),
        "risk": ("risk", "风险"),
        "redline": ("redline", "红线"),
        "full": ("full", "全面", "all"),
    }
    if normalized in review_scope_tokens:
        for opt in opts:
            picked = _option_answer_value(opt)
            if picked is None:
                continue
            text = _option_match_text(opt)
            if any(token in text for token in review_scope_tokens[normalized]):
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
    merged = _card_text_blob(card)
    if not merged:
        return []
    return _parse_missing_fields(merged)


def _card_text_blob(card: dict[str, Any], *, include_blocker_identity: bool = False) -> str:
    if not isinstance(card, dict) or not card:
        return ""
    parts: list[str] = []
    for key in ("title", "summary", "prompt"):
        token = str(card.get(key) or "").strip()
        if token:
            parts.append(token)
    if include_blocker_identity:
        for key in ("interruption_id", "interruption_key", "reason_kind", "reason_code", "product_type"):
            token = str(card.get(key) or "").strip()
            if token:
                parts.append(token)
    raw_questions = card.get("questions")
    questions: list[Any] = list(raw_questions) if isinstance(raw_questions, list) else []
    for row in questions:
        if not isinstance(row, dict):
            continue
        for key in ("question", "label", "placeholder", "field_key"):
            token = str(row.get(key) or "").strip()
            if token:
                parts.append(token)
    return " ".join(parts).strip()


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
    """Build a resume.answers payload from a card by applying overrides + safe defaults."""
    overrides = overrides or {}
    uploaded_file_ids = [str(x).strip() for x in (uploaded_file_ids or []) if str(x).strip()]

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
            if fk.endswith(".evidence_gap_stop_ask"):
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
            elif fk == "profile.decisions.contract_review_accepted_clause_ids":
                raw_options = q.get("options")
                options: list[Any] = raw_options if isinstance(raw_options, list) else []
                value = _pick_contract_review_clause_values(options)
            elif fk == "profile.decisions.contract_review_ignored_clause_ids":
                value = []
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
                value = default if has_default else None

        if value is None and required:
            raise AssertionError(f"pending_card_required_answer_missing:{fk}")

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
            value = override_value
            if value is None:
                continue
            _append_answer(fk, value)

    return {"answers": answers}


def card_signature(card: dict[str, Any]) -> str:
    interruption_type = str(card.get("type") or "").strip()
    interruption_id = str(card.get("interruption_id") or "").strip()
    interruption_key = str(card.get("interruption_key") or "").strip()
    reason_kind = str(card.get("reason_kind") or "").strip()
    reason_code = str(card.get("reason_code") or "").strip()
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
    raw = json.dumps(
        {
            "type": interruption_type,
            "interruption_id": interruption_id,
            "interruption_key": interruption_key,
            "reason_kind": reason_kind,
            "reason_code": reason_code,
            "questions": sigs,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
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


def _is_goal_completion_card(card: dict[str, Any]) -> bool:
    if not isinstance(card, dict) or not card:
        return False
    if _is_goal_completion_blocker(card):
        return True
    for row in (card.get("questions") if isinstance(card.get("questions"), list) else []):
        if not isinstance(row, dict):
            continue
        if str(row.get("field_key") or "").strip() == "data.workbench.goal":
            return True
    return False


def _compact_card_debug(card: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(card, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("type", "interruption_id", "interruption_key", "reason_kind", "reason_code", "product_type"):
        value = str(card.get(key) or "").strip()
        if value:
            out[key] = value
    prompt = _card_text_blob(card)
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
    prompt = _card_text_blob(card, include_blocker_identity=True)
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
    prompt = _card_text_blob(card)
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
    last_chat_run: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    strict_card_driven: bool = _STRICT_CARD_DRIVEN_DEFAULT
    matter_id: str | None = None
    session_status: str | None = None
    session_archived: bool = False
    seen_cards: list[dict[str, Any]] = field(default_factory=list)
    seen_card_signatures: list[str] = field(default_factory=list)
    seen_sse: list[dict[str, Any]] = field(default_factory=list)
    last_sse: dict[str, Any] | None = None
    progress_observer: ProgressObserver | None = None
    _repeat_unanswerable_signature: str | None = None
    _repeat_unanswerable_count: int = 0
    _repeat_card_signature: str | None = None
    _repeat_card_count: int = 0
    _last_step_used_nudge: bool = False
    async def _get_workflow_snapshot(self) -> dict[str, Any] | None:
        if not self.matter_id or not hasattr(self.client, "get_workflow_snapshot"):
            return None
        try:
            snapshot_resp = await self.client.get_workflow_snapshot(self.matter_id)
            snapshot_unwrapped = unwrap_api_response(snapshot_resp)
            return snapshot_unwrapped if isinstance(snapshot_unwrapped, dict) else None
        except Exception:
            return None

    async def _runtime_progress_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "session": self.session_id,
            "matter": str(self.matter_id or "").strip(),
            "status": str(self.session_status or "").strip(),
            "phase": "",
            "phase_status": "",
            "current_blocker": {},
            "blocker_label": "",
            "trace_node": "",
            "trace_status": "",
            "deliverables": "",
        }
        if not self.matter_id:
            return snapshot

        workflow_snapshot = await self._get_workflow_snapshot()
        if isinstance(workflow_snapshot, dict):
            blockers_view = _as_dict(workflow_snapshot.get("blockers_view"))
            current_blocker = _compact_blocker(blockers_view.get("current_blocker"))
            if current_blocker:
                snapshot["current_blocker"] = current_blocker
                snapshot["blocker_label"] = _blocker_label(current_blocker)

        phase_resp = await self.client.get_matter_phase_timeline(self.matter_id)
        phase_data = unwrap_api_response(phase_resp)
        if isinstance(phase_data, dict):
            raw_phases = phase_data.get("phases")
            phase_row = _resolve_current_phase_row(raw_phases)
            snapshot["phase"] = str(phase_row.get("phase_id") or phase_row.get("id") or "").strip()
            snapshot["phase_status"] = str(phase_row.get("status") or "").strip()

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
        blocker: dict[str, Any] | None = None,
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
        if snapshot.get("blocker_label"):
            parts.append(f"blocker={snapshot.get('blocker_label')}")
        if blocker:
            blocker_type = str(blocker.get("type") or "").strip()
            interruption_id = str(blocker.get("interruption_id") or "").strip()
            if blocker_type or interruption_id:
                parts.append(f"blocker_event={blocker_type}/{interruption_id}".rstrip("/"))
        event_summary = _compact_sse_events(sse)
        if event_summary:
            parts.append(f"events={event_summary}")
        if snapshot.get("deliverables"):
            parts.append(f"deliverables={snapshot.get('deliverables')}")
        _progress(" ".join(parts))
        if self.progress_observer is not None:
            observed = self.progress_observer(
                {
                    "label": label,
                    "step_no": step_no,
                    "max_steps": max_steps,
                    "session_id": snapshot.get("session"),
                    "matter_id": snapshot.get("matter"),
                    "session_status": snapshot.get("status"),
                    "phase": snapshot.get("phase"),
                    "phase_status": snapshot.get("phase_status"),
                    "current_blocker": snapshot.get("current_blocker"),
                    "trace_node": snapshot.get("trace_node"),
                    "trace_status": snapshot.get("trace_status"),
                    "deliverables": snapshot.get("deliverables"),
                    "event_summary": event_summary,
                    "blocker": blocker if isinstance(blocker, dict) else {},
                    "snapshot": snapshot,
                }
            )
            if asyncio.iscoroutine(observed):
                await observed

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

    async def get_current_blocker(self) -> dict[str, Any] | None:
        if self.session_archived:
            return None
        try:
            resp = await self.client.get_blocker(self.session_id)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 400 and e.response is not None:
                body = str(e.response.text or "")
                if "会话已归档" in body:
                    self.session_archived = True
                    self.session_status = "archived"
                    _debug("[flow] blocker fetch blocked: session archived")
                    return None
            if code in {400, 404, 409, 429, 500, 502, 503, 504}:
                _debug(f"[flow] blocker unavailable status={code}")
                return None
            raise
        except httpx.RequestError:
            _debug("[flow] blocker request error")
            return None
        blocker = unwrap_api_response(resp)
        if isinstance(blocker, dict) and blocker:
            _debug(
                f"[flow] blocker type={blocker.get('type')} interruption_id={blocker.get('interruption_id')} "
                f"reason={blocker.get('reason_kind')}:{blocker.get('reason_code')}"
            )
        return blocker if isinstance(blocker, dict) and blocker else None

    async def actionable_blocker_from_sse(self, sse: dict[str, Any] | None) -> dict[str, Any] | None:
        blocker = extract_last_blocker_from_sse(sse or {})
        if not isinstance(blocker, dict) or not blocker:
            return None
        authoritative = await self.get_current_blocker()
        if authoritative:
            return None
        return blocker

    async def resume_blocker(self, blocker: dict[str, Any], *, max_loops: int | None = None) -> dict[str, Any]:
        # Keep an audit trail for assertions/debugging.
        self.seen_cards.append(blocker)
        self.seen_card_signatures.append(card_signature(blocker))
        if _DEBUG:
            raw_questions = blocker.get("questions")
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
                f"[flow] blocker detail type={blocker.get('type')} interruption={blocker.get('interruption_id')} "
                f"fields={fields}"
            )
        answer_payload = auto_answer_card(blocker, overrides=self.overrides, uploaded_file_ids=self.uploaded_file_ids)
        _debug(
            f"[flow] resume blocker {blocker.get('interruption_id')} answers={len(answer_payload.get('answers') or [])} "
            f"payload={answer_payload}"
        )
        resolved_max_loops = _RESUME_MAX_LOOPS if max_loops is None else max(1, int(max_loops))
        settle_mode = "fire_and_poll" if _is_skill_error_confirm_card(blocker) else "first_event"
        resume_task = asyncio.create_task(
            self.client.resume(
                self.session_id,
                answer_payload,
                blocker=blocker,
                max_loops=resolved_max_loops,
                settle_mode=settle_mode,
            )
        )
        try:
            if _CARD_RESUME_SETTLE_TIMEOUT_S > 0:
                sse = await asyncio.wait_for(resume_task, timeout=_CARD_RESUME_SETTLE_TIMEOUT_S)
            else:
                sse = await resume_task
        except asyncio.TimeoutError:
            resume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resume_task
            raise
        if settle_mode == "full":
            try:
                assert_has_user_message(sse)
            except AssertionError:
                if not is_session_busy_sse(sse if isinstance(sse, dict) else {}):
                    raise
        else:
            if not _is_effective_resume_sse(sse if isinstance(sse, dict) else {}):
                try:
                    assert_has_user_message(sse)
                except AssertionError:
                    if not is_session_busy_sse(sse if isinstance(sse, dict) else {}):
                        raise
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        await self._emit_progress(label=f"resume:{settle_mode}", blocker=blocker, sse=sse)
        return sse

    async def nudge(
        self,
        text: str,
        *,
        attachments: list[str] | None = None,
        max_loops: int = 8,
        settle_mode: str = "full",
    ) -> dict[str, Any]:
        _debug(
            f"[flow] nudge text={text!r} attachments={len(attachments or [])} "
            f"max_loops={max_loops} settle_mode={settle_mode}"
        )
        sse = await self.client.chat(
            self.session_id,
            text,
            attachments=attachments or [],
            max_loops=max_loops,
            settle_mode=settle_mode,
        )
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        await self._emit_progress(label=f"nudge:{text[:24]}", sse=sse)
        return sse

    async def start_chat_run(
        self,
        *,
        entry_mode: str,
        service_type_id: str,
        delivery_goal: str,
        target_document_kind: str | None = None,
        supporting_document_kinds: list[str] | None = None,
        user_query: str = "",
        attachments: list[str] | None = None,
        max_loops: int = 12,
        silent: bool = True,
        settle_mode: str = "full",
        label: str | None = None,
    ) -> dict[str, Any]:
        normalized_entry_mode = str(entry_mode or "").strip()
        normalized_service_type_id = str(service_type_id or "").strip()
        normalized_delivery_goal = str(delivery_goal or "").strip()
        normalized_target_document_kind = str(target_document_kind or "").strip()
        normalized_supporting_document_kinds = [
            str(kind or "").strip()
            for kind in (supporting_document_kinds or [])
            if str(kind or "").strip()
        ]
        if normalized_entry_mode not in {"analysis", "direct_drafting"}:
            raise ValueError("start_chat_run requires entry_mode in {'analysis','direct_drafting'}")
        if not normalized_service_type_id:
            raise ValueError("start_chat_run requires service_type_id")
        if not normalized_delivery_goal:
            raise ValueError("start_chat_run requires delivery_goal")
        if normalized_entry_mode == "analysis" and normalized_target_document_kind:
            raise ValueError("analysis chat run must not carry target_document_kind")
        if normalized_entry_mode == "direct_drafting" and not normalized_target_document_kind:
            raise ValueError("direct_drafting chat run requires target_document_kind")
        self.last_chat_run = {
            "entry_mode": normalized_entry_mode,
            "service_type_id": normalized_service_type_id,
            "delivery_goal": normalized_delivery_goal,
            "target_document_kind": normalized_target_document_kind,
            "supporting_document_kinds": normalized_supporting_document_kinds,
        }
        request_label = label or (
            normalized_target_document_kind or normalized_delivery_goal or normalized_service_type_id
        )
        _debug(
            f"[flow] start_chat_run matter_bootstrap={self.last_chat_run!r} "
            f"attachments={len(attachments or self.uploaded_file_ids)} max_loops={max_loops} "
            f"settle_mode={settle_mode}"
        )
        sse = await self.client.start_chat_run(
            self.session_id,
            entry_mode=normalized_entry_mode,
            service_type_id=normalized_service_type_id,
            delivery_goal=normalized_delivery_goal,
            target_document_kind=normalized_target_document_kind or None,
            supporting_document_kinds=normalized_supporting_document_kinds,
            user_query=user_query,
            attachments=attachments or self.uploaded_file_ids,
            max_loops=max_loops,
            silent=silent,
            settle_mode=settle_mode,
        )
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        await self._emit_progress(label=f"chat_run:{request_label}", sse=sse)
        return sse

    async def step(
        self,
        *,
        stop_on_blocker: BlockerStopFn | None = None,
    ) -> dict[str, Any] | None:
        """Process one blocker if present; otherwise wait passively."""
        await self.refresh()
        if self.session_archived:
            # Avoid any chat/resume operations once the session is archived; keep run_until polling only.
            return {"events": [{"event": "session_archived"}], "output": "session archived"}
        card = await self.get_current_blocker()
        if not card and isinstance(self.last_sse, dict):
            card = await self.actionable_blocker_from_sse(self.last_sse)
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

            if stop_on_blocker is not None and stop_on_blocker(card):
                _debug(
                    f"[flow] stop_on_blocker interruption_id={card.get('interruption_id')} "
                    f"type={card.get('type')}"
                )
                self.seen_cards.append(card)
                self.seen_card_signatures.append(sig)
                return _blocker_intercept_sse(card)

            reason_code = str(card.get("reason_code") or "").strip()

            if reason_code == "retrieval_low_coverage" and self._repeat_card_count >= 3:
                if not self.strict_card_driven:
                    if not self.last_chat_run:
                        raise AssertionError("retrieval_low_coverage remediation requires last_chat_run")
                    _debug(f"[flow] retrieval_low_coverage remediation start_chat_run repeat={self._repeat_card_count}")
                    self._last_step_used_nudge = True
                    return await self.start_chat_run(
                        entry_mode=str(self.last_chat_run.get("entry_mode") or ""),
                        service_type_id=str(self.last_chat_run.get("service_type_id") or ""),
                        delivery_goal=str(self.last_chat_run.get("delivery_goal") or ""),
                        target_document_kind=str(self.last_chat_run.get("target_document_kind") or "") or None,
                        supporting_document_kinds=list(self.last_chat_run.get("supporting_document_kinds") or []),
                        max_loops=12,
                        settle_mode="fire_and_poll",
                        label="refresh_chat_run",
                    )

            if _is_unanswerable_card(card):
                if sig == self._repeat_unanswerable_signature:
                    self._repeat_unanswerable_count += 1
                else:
                    self._repeat_unanswerable_signature = sig
                    self._repeat_unanswerable_count = 1
                if self._repeat_unanswerable_count >= _UNANSWERABLE_CARD_MAX_REPEATS:
                    debug_card = _compact_card_debug(card)
                    raise AssertionError(
                        "workflow blocked by repeated unanswerable pending card: "
                        f"repeat={self._repeat_unanswerable_count}, card={debug_card}"
                    )
            else:
                self._repeat_unanswerable_signature = None
                self._repeat_unanswerable_count = 0
            _debug(f"[flow] resume blocker interruption_id={card.get('interruption_id')} type={card.get('type')}")
            return await self.resume_blocker(card)
        self._repeat_card_signature = None
        self._repeat_card_count = 0
        self._repeat_unanswerable_signature = None
        self._repeat_unanswerable_count = 0
        self._last_step_used_nudge = False
        return None

    async def run_until(
        self,
        predicate: Callable[["WorkbenchFlow"], Any],
        *,
        max_steps: int = 40,
        step_sleep_s: float = 0.0,
        description: str = "target condition",
        stop_on_blocker: BlockerStopFn | None = None,
    ) -> None:
        """Advance the workflow until predicate(flow) is truthy (sync/async)."""
        step_no = 0
        busy_retries = 0
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
            sse = await self.step(
                stop_on_blocker=stop_on_blocker,
            )
            if sse is None:
                await asyncio.sleep(max(_SESSION_BUSY_BACKOFF_S, 0.8))
                continue
            if is_session_busy_sse(sse):
                busy_retries += 1
                if busy_retries <= _SESSION_BUSY_EXTRA_RETRIES:
                    step_no -= 1
                    _debug(f"[flow] session busy; backoff {max(_SESSION_BUSY_BACKOFF_S, 0.5):.1f}s retry={busy_retries}")
                    await asyncio.sleep(max(_SESSION_BUSY_BACKOFF_S, 0.5))
                    continue
            else:
                busy_retries = 0
            if step_sleep_s:
                await asyncio.sleep(step_sleep_s)

        raise AssertionError(f"Failed to reach {description} after {max_steps} steps (session_id={self.session_id}, matter_id={self.matter_id})")


async def wait_for_initial_blocker(flow: WorkbenchFlow, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Wait until the workflow produces a blocker."""
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        await flow.refresh()
        last = await flow.get_current_blocker()
        if last:
            return last
        await asyncio.sleep(1.0)
    raise AssertionError(f"Timed out waiting for initial blocker (timeout={timeout_s}s, session_id={flow.session_id})")
