"""Session-driven workflow runner for lawyer workbench E2E.

This drives the real chain:
gateway -> consultations-service -> ai-engine (LangGraph) -> matter-service/files-service/templates/knowledge/memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .utils import trim, unwrap_api_response
from .sse import assert_has_user_message

_NUDGE_TEXT = "继续"
_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}


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


def _pick_recommended_or_first(options: list[Any]) -> Any | None:
    if not isinstance(options, list) or not options:
        return None
    for opt in options:
        if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
            return opt.get("value")
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") is not None:
            return opt.get("value")
    return None


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

    answers: list[dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))

        override_value = _resolve_override_value(fk, overrides)
        if override_value is not None:
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
            value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            value = default if has_default else _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            if has_default:
                value = default
            else:
                options = q.get("options") if isinstance(q.get("options"), list) else []
                # For multi-select questions, picking all recommended options can trigger a lot of expensive
                # downstream work (e.g., generating multiple documents). Prefer a minimal, deterministic choice.
                picked = None
                for opt in options:
                    if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
                        picked = opt.get("value")
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
                elif uploaded_file_ids and (required or skill_id == "system:kickoff"):
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
            else:
                value = default if has_default else ("已确认" if required else None)

        # For optional questions, omit the answer entirely if we don't have a value.
        # This avoids sending `null` into strict field validators (e.g. attachment_file_ids must be a list).
        if value is None and not required:
            continue

        answers.append({"field_key": fk, "value": value})

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


@dataclass
class WorkbenchFlow:
    client: Any
    session_id: str
    uploaded_file_ids: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    matter_id: str | None = None
    seen_cards: list[dict[str, Any]] = field(default_factory=list)
    seen_card_signatures: list[str] = field(default_factory=list)
    seen_sse: list[dict[str, Any]] = field(default_factory=list)
    last_sse: dict[str, Any] | None = None

    async def refresh(self) -> None:
        sess = unwrap_api_response(await self.client.get_session(self.session_id))
        if isinstance(sess, dict) and sess.get("matter_id") is not None:
            self.matter_id = str(sess.get("matter_id")).strip()

    async def get_pending_card(self) -> dict[str, Any] | None:
        resp = await self.client.get_pending_card(self.session_id)
        card = unwrap_api_response(resp)
        return card if isinstance(card, dict) and card else None

    async def resume_card(self, card: dict[str, Any]) -> dict[str, Any]:
        # Keep an audit trail for assertions/debugging.
        self.seen_cards.append(card)
        self.seen_card_signatures.append(card_signature(card))
        skill_id = str(card.get("skill_id") or "").strip()
        if skill_id == "system:kickoff":
            # Use /resume to submit the kickoff card deterministically.
            # Relying on "auto-resume from chat" is sensitive to max_loops and can appear stuck.
            user_response = auto_answer_card(card, overrides=self.overrides, uploaded_file_ids=self.uploaded_file_ids)
            sse = await self.client.resume(self.session_id, user_response, pending_card=card, max_loops=_RESUME_MAX_LOOPS)
            assert_has_user_message(sse)
            if isinstance(sse, dict):
                self.last_sse = sse
                self.seen_sse.append(sse)
            return sse

        user_response = auto_answer_card(card, overrides=self.overrides, uploaded_file_ids=self.uploaded_file_ids)
        sse = await self.client.resume(self.session_id, user_response, pending_card=card, max_loops=_RESUME_MAX_LOOPS)
        assert_has_user_message(sse)
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        return sse

    async def nudge(self, text: str = _NUDGE_TEXT, *, attachments: list[str] | None = None, max_loops: int = 12) -> dict[str, Any]:
        sse = await self.client.chat(self.session_id, text, attachments=attachments or [], max_loops=max_loops)
        if isinstance(sse, dict):
            self.last_sse = sse
            self.seen_sse.append(sse)
        return sse

    async def step(self, *, nudge_text: str = _NUDGE_TEXT) -> dict[str, Any] | None:
        """Process one pending card if exists; otherwise send a small nudge chat."""
        await self.refresh()
        card = await self.get_pending_card()
        if card:
            _debug(f"[flow] resume card skill_id={card.get('skill_id')} task_key={card.get('task_key')}")
            return await self.resume_card(card)
        _debug(f"[flow] nudge {nudge_text!r}")
        return await self.nudge(nudge_text, attachments=[], max_loops=12)

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
        for i in range(1, max_steps + 1):
            ok = predicate(self)
            if asyncio.iscoroutine(ok):
                ok = await ok
            if ok:
                _debug(f"[flow] reached {description} at step {i} (session_id={self.session_id}, matter_id={self.matter_id})")
                return
            _debug(f"[flow] step {i}/{max_steps} waiting for {description} (session_id={self.session_id}, matter_id={self.matter_id})")
            await self.step(nudge_text=nudge_text)
            if step_sleep_s:
                await asyncio.sleep(step_sleep_s)
        raise AssertionError(f"Failed to reach {description} after {max_steps} steps (session_id={self.session_id}, matter_id={self.matter_id})")


async def wait_for_initial_card(flow: WorkbenchFlow, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Wait until the workflow produces a pending card (kickoff/intake/etc)."""
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        await flow.refresh()
        last = await flow.get_pending_card()
        if last:
            return last
        await asyncio.sleep(1.0)
    raise AssertionError(f"Timed out waiting for initial card (timeout={timeout_s}s, session_id={flow.session_id})")
