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

_NUDGE_TEXT = "继续"
_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}
_AUTO_NUDGE = str(os.getenv("E2E_AUTO_NUDGE", "1") or "").strip().lower() in {"1", "true", "yes"}


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


def extract_last_card_from_sse(sse: dict[str, Any]) -> dict[str, Any] | None:
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for it in reversed(events):
        if not isinstance(it, dict):
            continue
        if it.get("event") != "card":
            continue
        data = it.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def is_session_busy_sse(sse: dict[str, Any] | None) -> bool:
    if not isinstance(sse, dict):
        return False
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for it in events:
        if not isinstance(it, dict):
            continue
        event_name = str(it.get("event") or "").strip()
        if event_name == "session_busy":
            return True
        if event_name == "error":
            data = it.get("data") if isinstance(it.get("data"), dict) else {}
            msg = " ".join([str(data.get("message") or ""), str(data.get("error") or "")]).strip().lower()
            if not msg:
                continue
            if (
                ("session busy" in msg)
                or ("会话正在处理中" in msg)
                or ("上一轮完成后再发送" in msg)
                or ("already processing" in msg)
            ):
                return True
    output = str(sse.get("output") or "").strip()
    if not output:
        return False
    return ("会话正在处理中" in output) or ("session busy" in output.lower())


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

    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        allowed_field_keys.add(fk)
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))

        override_value = _resolve_override_value(fk, overrides)
        if override_value is not None:
            if fk in {"profile.review_scope", "review_scope"}:
                override_value = _coerce_review_scope_for_options(
                    override_value,
                    q.get("options") if isinstance(q.get("options"), list) else [],
                )
            answers.append({"field_key": fk, "value": override_value})
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
            else:
                value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            value = default if has_default else _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
            if fk in {"profile.review_scope", "review_scope"}:
                value = _coerce_review_scope_for_options(
                    value,
                    q.get("options") if isinstance(q.get("options"), list) else [],
                )
        elif it in {"multi_select", "multiple_select"}:
            if has_default:
                value = default
            else:
                options = q.get("options") if isinstance(q.get("options"), list) else []
                # For multi-select questions, picking all recommended options can trigger a lot of expensive
                # downstream work (e.g., generating multiple documents). Prefer a minimal, deterministic choice.
                picked = None
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

        # For optional questions, omit the answer entirely if we don't have a value.
        # This avoids sending `null` into strict field validators (e.g. attachment_file_ids must be a list).
        if value is None and not required:
            continue

        answers.append({"field_key": fk, "value": value})

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
            answers.append({"field_key": fk, "value": value})

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

    return {"answers": answers}


def card_signature(card: dict[str, Any]) -> str:
    skill = str(card.get("skill_id") or "").strip()
    task = str(card.get("task_key") or "").strip()
    review = str(card.get("review_type") or "").strip()
    qs = card.get("questions") if isinstance(card.get("questions"), list) else []
    sigs = []
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

    async def resume_card(self, card: dict[str, Any]) -> dict[str, Any]:
        # Keep an audit trail for assertions/debugging.
        self.seen_cards.append(card)
        self.seen_card_signatures.append(card_signature(card))
        if _DEBUG:
            qs = card.get("questions") if isinstance(card.get("questions"), list) else []
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
        sse = await self.client.resume(self.session_id, user_response, pending_card=card, max_loops=_RESUME_MAX_LOOPS)
        assert_has_user_message(sse)
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        return sse

    async def nudge(self, text: str = _NUDGE_TEXT, *, attachments: list[str] | None = None, max_loops: int = 8) -> dict[str, Any]:
        _debug(f"[flow] nudge text={text!r} attachments={len(attachments or [])} max_loops={max_loops}")
        sse = await self.client.chat(self.session_id, text, attachments=attachments or [], max_loops=max_loops)
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        return sse

    async def step(self, *, nudge_text: str = _NUDGE_TEXT, allow_nudge: bool = True) -> dict[str, Any] | None:
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
                _debug(f"[flow] reached {description} at step {step_no + 1} (session_id={self.session_id}, matter_id={self.matter_id})")
                return

            step_no += 1
            _debug(f"[flow] step {step_no}/{max_steps} waiting for {description} (session_id={self.session_id}, matter_id={self.matter_id})")
            allow_nudge = (suppress_nudge_rounds <= 0) and (nudge_cooldown <= 0)
            sse = await self.step(nudge_text=nudge_text, allow_nudge=allow_nudge)
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
