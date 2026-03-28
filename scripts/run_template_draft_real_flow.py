"""Run smart-template drafting end-to-end via consultations-service WebSocket (real LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from support.workbench.docx import (
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from support.workbench.flow_runner import (
    WorkbenchFlow,
    extract_last_card_from_sse,
    is_session_busy_sse,
)
from support.workbench.sse import assert_visible_response
from support.workbench.utils import eventually, unwrap_api_response

from scripts._support.template_draft_real_flow_support import (
    DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE,
    DEFAULT_LEGAL_OPINION_FACTS,
    DOCGEN_STOP_NODES,
    _build_node_timeline_row,
    _detect_docgen_node,
    _extend_docgen_node_sequence,
    _extract_docgen_snapshot,
    _is_stop_node_reached,
    _normalize_stop_node,
)
from scripts._support.flow_score_support import build_template_flow_scores
from scripts._support.diagnostic_bundle_support import export_observability_bundle
from scripts._support.quality_policy_support import build_bundle_quality_reports
from scripts._support.workflow_real_flow_support import collect_ai_debug_refs, configure_direct_service_mode, load_real_flow_env, terminate_stale_script_runs


DEFAULT_FACTS = DEFAULT_LEGAL_OPINION_FACTS

DEFAULT_EVIDENCE_RELATIVE = DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE

DEFAULT_SERVICE_TYPE_ID = "document_drafting"
REASONABLE_CARD_KINDS = {"clarify", "select", "confirm"}
LOW_SIGNAL_HINTS = (
    "处理中",
    "正在处理",
    "请稍候",
    "稍后",
    "session busy",
    "会话正在处理中",
)
CITATION_RE = re.compile(r"《[^》]{2,40}》第[一二三四五六七八九十百千万0-9]{1,8}条")
PARTY_RE = re.compile(r"(?:^|\n)\s*(?:原告|被告|申请人|被申请人|上诉人|被上诉人|委托人|相对方|甲方|乙方|买方|卖方)\s*[:：]\s*([^\n，,。；;]{1,48})")
AMOUNT_RE = re.compile(r"(?:人民币)?\s*(\d{2,10})(?=\s*(?:万元|元))")
CLAIM_RE = re.compile(r"(?:^|\n)\s*(?:诉求|目标|需求)\s*[:：]\s*([^\n]+)")
CLAIM_KEYWORDS = ("返还", "支付", "逾期利息", "诉讼费", "赔偿", "承担", "评估", "建议", "风险", "保全", "谈判", "解除", "索赔")
PARTY_LINE_RE = re.compile(r"^\s*(原告|被告|申请人|被申请人|上诉人|被上诉人|委托人|相对方|甲方|乙方|买方|卖方)\s*[:：]")
FORUM_RE = re.compile(r"([^\n，。；;]{2,40}(?:仲裁委员会|人民法院))")
UNRESOLVED_QUALITY_TOKENS = (
    "待核实",
    "待确认",
    "输出格式",
    "法院名称待核实",
    "具体法院名称待核实",
)
GENERIC_LEGAL_PHRASES = (
    "相关法律规定",
    "依据有关法律规定",
)
BARE_ARTICLE_RULE_RE = re.compile(r"(?<!》)第[一二三四五六七八九十百千万零〇0-9]{1,8}条规定")


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _event_counts(sse: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    events_obj = sse.get("events")
    events: list[Any] = events_obj if isinstance(events_obj, list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        name = _safe_str(row.get("event")) or "unknown"
        out[name] = int(out.get(name) or 0) + 1
    return out


def _sse_has_user_message_event(sse: dict[str, Any]) -> bool:
    events_obj = sse.get("events")
    events: list[Any] = events_obj if isinstance(events_obj, list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        if _safe_str(row.get("event")) == "user_message":
            return True
    return False


def _sse_error_events(sse: dict[str, Any]) -> list[dict[str, Any]]:
    events_obj = sse.get("events")
    events: list[Any] = events_obj if isinstance(events_obj, list) else []
    out: list[dict[str, Any]] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        if _safe_str(row.get("event")) != "error":
            continue
        data = row.get("data")
        out.append(data if isinstance(data, dict) else {"raw": data})
    return out


def _extract_templates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = unwrap_api_response(payload)
    if isinstance(data, dict):
        templates_obj = data.get("templates")
        templates: list[Any] = templates_obj if isinstance(templates_obj, list) else []
        if templates:
            return [t for t in templates if isinstance(t, dict)]
    if isinstance(payload, dict):
        payload_templates_obj = payload.get("templates")
        payload_templates: list[Any] = payload_templates_obj if isinstance(payload_templates_obj, list) else []
        if payload_templates:
            return [t for t in payload_templates if isinstance(t, dict)]
    return []


def _extract_last_cards(sse: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    events_obj = sse.get("events")
    events: list[Any] = events_obj if isinstance(events_obj, list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        if _safe_str(row.get("event")) != "card":
            continue
        data = row.get("data")
        if isinstance(data, dict) and data:
            cards.append(data)
    return cards


def _card_kind(card: dict[str, Any]) -> str:
    for key in ("review_type", "card_type", "type"):
        value = _safe_str(card.get(key)).lower()
        if value:
            return value
    return ""


def _is_low_signal_output(text: str) -> bool:
    raw = _safe_str(text)
    if not raw:
        return True
    short = raw.replace("\n", " ")[:120].lower()
    if len(short) <= 18 and any(tok in short for tok in LOW_SIGNAL_HINTS):
        return True
    if all(tok in short for tok in ("会话", "处理中")):
        return True
    return False


def _is_terminal_failure_output(text: str) -> bool:
    raw = _safe_str(text)
    if not raw:
        return False
    return ("技能执行失败（" in raw and "已停止自动循环" in raw) or ("请处理阻塞后继续" in raw)


def _resume_ack_only_response(*, action: str, sse: dict[str, Any], visible_error: str) -> bool:
    if not str(action or "").startswith("resume"):
        return False
    err = _safe_str(visible_error)
    if not err:
        return False
    event_names = set(_event_counts(sse).keys())
    allowed = {"user_message", "progress", "usage", "error", "end"}
    if not event_names or not event_names.issubset(allowed):
        return False
    if "SSE missing progress events" in err:
        return True
    if "SSE missing end/complete" in err:
        # Some resume-card flows only emit an acknowledgement + heartbeat/progress,
        # then continue work asynchronously. Treat this as an ack-only success and
        # let the main polling loop pick up the next pending card or deliverable.
        return "user_message" in event_names and "progress" in event_names
    if "SSE returned error events" in err and "closed" in err.lower():
        return True
    return False


def _kickoff_ack_only_response(*, action: str, sse: dict[str, Any], visible_error: str) -> bool:
    if action != "chat.kickoff":
        return False
    err = _safe_str(visible_error)
    if "SSE missing end/complete" not in err:
        return False
    event_names = set(_event_counts(sse).keys())
    allowed = {"user_message", "progress", "usage"}
    if not event_names or not event_names.issubset(allowed):
        return False
    return "progress" in event_names


def _workflow_action_ack_only_response(*, action: str, sse: dict[str, Any]) -> bool:
    if action != "workflow_action.template_draft_start":
        return False
    errors = _sse_error_events(sse)
    if not errors:
        return False
    for row in errors:
        message = _safe_str((row or {}).get("message"))
        partial = bool((row or {}).get("partial"))
        if (not partial) or ("后台继续处理中" not in message):
            return False
    return True


def _normalize_text_for_number_match(text: str) -> str:
    return re.sub(r"[\s,，]", "", text or "")


def _build_doc_targets(facts: str) -> dict[str, Any]:
    parties: list[str] = []
    for m in PARTY_RE.finditer(facts or ""):
        name = _safe_str(m.group(1))
        if name and name not in parties:
            parties.append(name)
    amounts: list[str] = []
    for m in AMOUNT_RE.finditer(facts or ""):
        amount = _safe_str(m.group(1))
        if amount and amount not in amounts:
            amounts.append(amount)
    claim_text = ""
    m_claim = CLAIM_RE.search(facts or "")
    if m_claim:
        claim_text = _safe_str(m_claim.group(1))
    keyword_hits = [k for k in CLAIM_KEYWORDS if k in claim_text]
    return {
        "parties": parties[:4],
        "amounts": amounts[:4],
        "claim_keywords": keyword_hits[:6],
    }


def _resolve_evidence_paths(extra_paths: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    use_default = len([_safe_str(x) for x in extra_paths if _safe_str(x)]) == 0
    if use_default:
        for rel in DEFAULT_EVIDENCE_RELATIVE:
            p = (E2E_ROOT / rel).resolve()
            if p.exists() and p.is_file():
                key = str(p)
                if key not in seen:
                    seen.add(key)
                    out.append(p)

    for raw in extra_paths:
        s = _safe_str(raw)
        if not s:
            continue
        p = Path(s).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"evidence file not found: {p}")
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    return out


def _load_facts_text(args: argparse.Namespace) -> str:
    if _safe_str(args.facts_file):
        p = Path(args.facts_file).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"facts file not found: {p}")
        text = p.read_text(encoding="utf-8")
        if _safe_str(text):
            return text
    return DEFAULT_FACTS


def _build_flow_overrides(
    facts_text: str,
    uploaded_file_ids: list[str],
    *,
    service_type_id: str,
    template_name: str,
) -> dict[str, Any]:
    def _extract_forum_name(text: str) -> str:
        raw = _safe_str(text)
        if not raw:
            return ""
        hit = FORUM_RE.search(raw)
        return _safe_str(hit.group(1)) if hit else ""

    claim_text = ""
    m_claim = CLAIM_RE.search(facts_text or "")
    if m_claim:
        claim_text = _safe_str(m_claim.group(1))
    summary_line = _safe_str(facts_text).replace("\n", " ")
    if len(summary_line) > 140:
        summary_line = summary_line[:140].rstrip() + "…"

    party_lines: list[str] = []
    for line in str(facts_text or "").splitlines():
        item = _safe_str(line)
        if not item:
            continue
        if PARTY_LINE_RE.search(item):
            party_lines.append(item)
    parties_text = "\n".join(party_lines[:2]) if party_lines else "原告：张三。被告：李四。"

    facts_lines = [_safe_str(line) for line in str(facts_text or "").splitlines() if _safe_str(line)]
    background_lines: list[str] = []
    preferred_lines: list[str] = []
    for line in facts_lines:
        if PARTY_LINE_RE.search(line):
            continue
        if line.startswith("文书类型："):
            continue
        if (
            line.startswith("事项：")
            or line.startswith("争点：")
            or line.startswith("时间线：")
            or line.startswith("证据线索：")
            or line.startswith("目标：")
            or line.startswith("- ")
            or bool(re.match(r"^\d+[.、]", line))
        ):
            preferred_lines.append(line)
        background_lines.append(line)
    if preferred_lines:
        background_lines = preferred_lines
    background_text = "\n".join(background_lines).strip()
    if not background_text:
        background_text = _safe_str(facts_text)
    if len(background_text) > 1200:
        background_text = background_text[:1200].rstrip() + "…"

    forum_name = _extract_forum_name(facts_text) or "北京市海淀区人民法院"

    return {
        "profile.facts": _safe_str(facts_text),
        "profile.background": background_text,
        "profile.parties": parties_text,
        "profile.summary": summary_line or "请基于已上传材料生成案件摘要。",
        "profile.claims": claim_text or "请按已提供事实整理诉求并推进起草。",
        "profile.court_name": forum_name,
        "profile.document_type": _safe_str(template_name) or "民事起诉状",
        "profile.service_type_id": _safe_str(service_type_id) or DEFAULT_SERVICE_TYPE_ID,
        "attachment_file_ids": [str(x).strip() for x in uploaded_file_ids if _safe_str(x)],
    }


async def _resolve_template_name(client: ApiClient, template_id: str, preferred_name: str) -> str:
    if _safe_str(preferred_name):
        return _safe_str(preferred_name)

    lookup_timeout_s = max(1.0, float(os.getenv("E2E_TEMPLATE_LOOKUP_TIMEOUT_S", "5") or 5))

    try:
        payload = await client.get(
            "/templates-service/atomic/templates",
            timeout=lookup_timeout_s,
            get_retries=1,
        )
        templates = _extract_templates(payload)
        for row in templates:
            if _safe_str(row.get("id")) != template_id:
                continue
            name = _safe_str(row.get("name"))
            if name:
                return name
    except Exception:
        pass

    try:
        detail = await client.get(
            f"/templates-service/templates/{template_id}",
            timeout=lookup_timeout_s,
            get_retries=1,
        )
        data = unwrap_api_response(detail)
        if isinstance(data, dict):
            name = _safe_str(data.get("name"))
            if name:
                return name
    except Exception:
        pass

    return f"模板#{template_id}"


async def _list_deliverables(client: ApiClient, matter_id: str, output_key: str) -> list[dict[str, Any]]:
    try:
        resp = await client.list_deliverables(matter_id, output_key=output_key)
    except Exception:
        return []
    data = unwrap_api_response(resp)
    rows_obj = data.get("deliverables") if isinstance(data, dict) else None
    rows: list[Any] = rows_obj if isinstance(rows_obj, list) else []
    return [row for row in rows if isinstance(row, dict)]


async def _first_deliverable_with_file(client: ApiClient, matter_id: str, output_key: str) -> dict[str, Any] | None:
    rows = await _list_deliverables(client, matter_id, output_key)
    for row in rows:
        if _safe_str(row.get("file_id")):
            return row
    return None


def _pick_deliverable_with_file(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if _safe_str(row.get("file_id")):
            return row
    return None


def _deliverable_signature(row: dict[str, Any] | None) -> str:
    if not isinstance(row, dict):
        return ""
    return "|".join(
        [
            _safe_str(row.get("status")),
            _safe_str(row.get("file_id")),
            _safe_str(row.get("updated_at")),
            _safe_str(row.get("version")),
            _safe_str(row.get("analysis_version")),
        ]
    )


def _snapshot_requires_docgen_settle(snapshot: dict[str, Any] | None) -> bool:
    snap = snapshot if isinstance(snapshot, dict) else {}
    phase = _safe_str(snap.get("current_phase")).lower()
    task_id = _safe_str(snap.get("current_task_id")).lower()
    docgen_node = _safe_str(snap.get("docgen_node")).lower()
    deliverable = snap.get("deliverable") if isinstance(snap.get("deliverable"), dict) else {}
    deliverable_status = _safe_str(deliverable.get("status")).lower()
    quality_decision = _safe_str(snap.get("quality_review_decision")).lower()

    if phase == "docgen":
        return True
    if task_id.startswith("docgen") or task_id.startswith("document_drafting_docgen") or task_id.startswith("document_generation"):
        return True
    if docgen_node and docgen_node not in {"finish", "sync", "done", "completed"}:
        return True
    if quality_decision in {"repair", "review"}:
        return True
    if deliverable_status and deliverable_status not in {"completed", "archived", "done"}:
        return True
    return False


def _deliverable_candidate_settled(
    *,
    snapshot: dict[str, Any] | None,
    stable_polls: int,
    seen_for_s: float,
    min_stable_polls: int,
    settle_grace_s: float,
) -> bool:
    if stable_polls < max(1, int(min_stable_polls)):
        return False
    if _snapshot_requires_docgen_settle(snapshot) and seen_for_s < max(0.0, float(settle_grace_s)):
        return False
    return True


async def _create_matter_with_retry(
    client: ApiClient,
    *,
    service_type_id: str,
    title: str,
    file_ids: list[str],
    max_attempts: int = 6,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        attempt_title = title if attempt == 1 else f"{title}-retry{attempt}"
        try:
            return await client.create_matter(service_type_id=service_type_id, title=attempt_title, file_ids=file_ids)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if code in {409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                await asyncio.sleep(min(2.5, 0.4 * attempt))
                last_error = e
                continue
            raise
        except httpx.RequestError as e:
            if attempt < max_attempts:
                await asyncio.sleep(min(2.5, 0.4 * attempt))
                last_error = e
                continue
            raise
    raise last_error if last_error else RuntimeError("create_matter failed")


async def _create_session_with_retry(client: ApiClient, matter_id: str, max_attempts: int = 6) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return await client.create_session(matter_id=matter_id)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if code in {404, 409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                await asyncio.sleep(min(2.5, 0.4 * attempt))
                last_error = e
                continue
            raise
        except httpx.RequestError as e:
            if attempt < max_attempts:
                await asyncio.sleep(min(2.5, 0.4 * attempt))
                last_error = e
                continue
            raise
    raise last_error if last_error else RuntimeError("create_session failed")


async def _wait_template_draft_start_settled(
    client: ApiClient,
    *,
    session_id: str,
    matter_id: str,
    timeout_s: float = 20.0,
    poll_interval_s: float = 1.5,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout_s)
    last_pending: dict[str, Any] = {}
    last_snapshot: dict[str, Any] = {}

    while asyncio.get_running_loop().time() < deadline:
        try:
            pending_resp = await client.get_pending_card(session_id)
            pending_data = unwrap_api_response(pending_resp)
            if isinstance(pending_data, dict):
                last_pending = pending_data
                if pending_data:
                    return {
                        "settled": True,
                        "reason": "pending_card",
                        "pending_card": pending_data,
                        "snapshot": last_snapshot,
                    }
        except Exception:
            pass

        try:
            snapshot_resp = await client.get(f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot")
            snapshot_data = unwrap_api_response(snapshot_resp)
            if isinstance(snapshot_data, dict):
                last_snapshot = snapshot_data
                analysis_state_obj = snapshot_data.get("analysis_state")
                analysis_state = analysis_state_obj if isinstance(analysis_state_obj, dict) else {}
                current_subgraph = _safe_str(analysis_state.get("current_subgraph"))
                current_task_id = _safe_str(analysis_state.get("current_task_id"))
                if current_subgraph == "document_generation" or current_task_id.startswith("docgen"):
                    return {
                        "settled": True,
                        "reason": "docgen_runtime",
                        "pending_card": last_pending,
                        "snapshot": snapshot_data,
                    }
        except Exception:
            pass

        await asyncio.sleep(max(0.5, poll_interval_s))

    return {
        "settled": False,
        "reason": "timeout",
        "pending_card": last_pending,
        "snapshot": last_snapshot,
    }


def _evaluate_dialogue_quality(
    *,
    rounds: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    strict_dialogue: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    busy_rounds = sum(1 for r in rounds if bool(r.get("busy")))
    visible_failures = [r for r in rounds if (not bool(r.get("busy"))) and (not bool(r.get("visible_ok")))]
    low_signal_max = max((int(r.get("low_signal_streak") or 0) for r in rounds), default=0)

    card_kinds: list[str] = []
    for card in cards:
        kind = _card_kind(card)
        if kind:
            card_kinds.append(kind)

    kind_set = set(card_kinds)
    has_reasonable_card_type = bool(kind_set.intersection(REASONABLE_CARD_KINDS))
    cardless_success = (not cards) and (not visible_failures) and bool(rounds)

    if not cards and not cardless_success:
        failures.append("未观察到可交互卡片，无法证明对话式起草链路可用")
    if cards and not has_reasonable_card_type:
        failures.append(
            "已观察到卡片，但未命中 clarify/select/confirm 典型交互类型（可能为环境差异或链路退化）"
        )
    if visible_failures:
        failures.append(f"存在 {len(visible_failures)} 轮不可见响应（无有效输出/卡片或事件结构异常）")

    passed = len(failures) == 0
    if strict_dialogue and not passed:
        failures.append("strict_dialogue 已开启：对话合理性未达标")

    return {
        "strict_dialogue": strict_dialogue,
        "pass": passed,
        "failure_reasons": failures,
        "round_count": len(rounds),
        "busy_round_count": busy_rounds,
        "visible_failure_count": len(visible_failures),
        "max_low_signal_streak": low_signal_max,
        "card_count": len(cards),
        "card_types": sorted(kind_set),
        "has_reasonable_card_type": has_reasonable_card_type,
        "cardless_success": cardless_success,
    }


def _evaluate_document_quality(
    *,
    text: str,
    targets: dict[str, Any],
    min_citations: int,
    deliverable_status: str,
    strict_quality: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    placeholder_leak = False
    try:
        assert_docx_has_no_template_placeholders(text)
    except AssertionError as e:
        placeholder_leak = True
        failures.append(str(e))

    unresolved_hits = [token for token in UNRESOLVED_QUALITY_TOKENS if token in (text or "")]
    if unresolved_hits:
        failures.append(f"文书包含未完成占位/待核实表述: {', '.join(unresolved_hits)}")

    generic_legal_hits = [token for token in GENERIC_LEGAL_PHRASES if token in (text or "")]
    if generic_legal_hits:
        failures.append(f"文书包含泛化法律表述: {', '.join(generic_legal_hits)}")

    if BARE_ARTICLE_RULE_RE.search(text or ""):
        failures.append("文书存在未指明法名的条文表述（如“第X条规定”）")

    parties = [p for p in targets.get("parties") or [] if _safe_str(p)]
    amounts = [a for a in targets.get("amounts") or [] if _safe_str(a)]
    claim_keywords = [k for k in targets.get("claim_keywords") or [] if _safe_str(k)]

    party_missing = [p for p in parties if p not in text]
    if party_missing:
        failures.append(f"当事人命中不足: {party_missing}")

    normalized_text = _normalize_text_for_number_match(text)
    amount_missing = []
    for amount in amounts:
        target = _normalize_text_for_number_match(amount)
        if target and target not in normalized_text:
            amount_missing.append(amount)
    if amount_missing:
        failures.append(f"核心金额命中不足: {amount_missing}")

    claim_hits = [k for k in claim_keywords if k in text]
    claim_required = min(2, len(claim_keywords))
    if claim_required > 0 and len(claim_hits) < claim_required:
        failures.append(f"诉求关键词命中不足: hits={claim_hits}, expected>={claim_required}")

    citations = CITATION_RE.findall(text)
    if len(citations) < max(0, int(min_citations)):
        failures.append(f"法条引用数量不足: {len(citations)} < {min_citations}")

    terminal_statuses = {"archived", "completed", "done"}
    if _safe_str(deliverable_status).lower() not in terminal_statuses:
        failures.append(f"交付物状态未达到终态: status={deliverable_status}")

    hit_total = len(parties) + len(amounts) + len(claim_keywords)
    hit_count = (len(parties) - len(party_missing)) + (len(amounts) - len(amount_missing)) + len(claim_hits)
    coverage = 100.0 if hit_total <= 0 else round((hit_count / hit_total) * 100.0, 2)

    passed = len(failures) == 0
    if strict_quality and not passed:
        failures.append("strict_quality 已开启：文书质量未达高质量交付门槛")

    return {
        "strict_quality": strict_quality,
        "pass": passed,
        "failure_reasons": failures,
        "placeholder_leak": placeholder_leak,
        "citation_count": len(citations),
        "citation_threshold": int(min_citations),
        "fact_coverage_score": coverage,
        "party_expected": parties,
        "party_missing": party_missing,
        "amount_expected": amounts,
        "amount_missing": amount_missing,
        "claim_keywords_expected": claim_keywords,
        "claim_keywords_hit": claim_hits,
        "deliverable_status": deliverable_status,
        "document_length": len(text or ""),
    }


def _write_events_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def _http_error_details(exc: Exception) -> dict[str, Any]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return {"error": str(exc)}
    resp = exc.response
    body = ""
    try:
        body = resp.text if resp is not None else ""
    except Exception:
        body = ""
    return {
        "error": str(exc),
        "status_code": resp.status_code if resp is not None else None,
        "url": str(resp.request.url) if (resp is not None and resp.request is not None) else "",
        "response_text": body[:4000],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class StopAfterNodeReached(RuntimeError):
    def __init__(self, snapshot: dict[str, Any], target_node: str):
        self.snapshot = snapshot
        self.target_node = target_node
        super().__init__(f"stop_after_node reached: {target_node}")


async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)
    terminate_stale_script_runs(script_name="run_template_draft_real_flow.py")

    direct_mode = bool(args.direct_local) or not bool(args.use_gateway)
    direct_config: dict[str, str] = {}
    if direct_mode:
        base_url, direct_config = configure_direct_service_mode(
            remote_stack_host=_safe_str(args.remote_stack_host),
            consultations_base_url=_safe_str(args.consultations_base_url),
            matter_base_url=_safe_str(args.matter_base_url),
            files_base_url=_safe_str(args.files_base_url),
            templates_base_url=_safe_str(args.templates_base_url),
            local_consultations=True,
            local_matter=True,
            local_templates=True,
            direct_user_id=_safe_str(args.direct_user_id),
            direct_org_id=_safe_str(args.direct_org_id),
            direct_is_superuser="false",
        )
    else:
        base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    template_id = _safe_str(args.template_id)
    if not template_id:
        raise ValueError("template_id is required")

    facts_text = _load_facts_text(args)
    doc_targets = _build_doc_targets(facts_text)
    output_key = _safe_str(args.output_key) or f"template:{template_id}"
    effective_service_type = _safe_str(args.service_type_id) or DEFAULT_SERVICE_TYPE_ID
    if effective_service_type != DEFAULT_SERVICE_TYPE_ID:
        raise ValueError(
            f"template_draft_start only allowed for {DEFAULT_SERVICE_TYPE_ID} matters; got={effective_service_type}"
        )
    raw_stop_after_node = _safe_str(args.stop_after_node)
    stop_after_node = _normalize_stop_node(raw_stop_after_node)
    if raw_stop_after_node and not stop_after_node:
        raise ValueError(f"unsupported stop-after-node: {raw_stop_after_node}; choices={sorted(DOCGEN_STOP_NODES)}")
    poll_interval_s = max(0.3, float(args.poll_interval_s))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_token = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = (
        Path(args.output_dir).expanduser()
        if _safe_str(args.output_dir)
        else REPO_ROOT / f"output/template-draft-chain/{ts}"
    ).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cause_anchor_path: Path | None = None
    if _safe_str(args.cause_anchor_file):
        cause_anchor_path = Path(args.cause_anchor_file).expanduser().resolve()
        if not cause_anchor_path.exists() or not cause_anchor_path.is_file():
            raise FileNotFoundError(f"cause anchor file not found: {cause_anchor_path}")

    print(f"[config] base_url={base_url}")
    print(f"[config] user={username}")
    print(f"[config] service_type_id={effective_service_type}")
    print(f"[config] template_id={template_id}")
    print(f"[config] output_key={output_key}")
    print(f"[config] output_dir={out_dir}")
    print(f"[config] direct_service_mode={direct_mode}")
    if direct_mode:
        print(f"[config] auth_base_url={direct_config.get('auth_base_url') or '-'}")
        print(f"[config] consultations_base_url={direct_config.get('consultations_base_url') or '-'}")
        print(f"[config] matter_base_url={direct_config.get('matter_base_url') or '-'}")
        print(f"[config] files_base_url={direct_config.get('files_base_url') or '-'}")
        print(f"[config] templates_base_url={direct_config.get('templates_base_url') or '-'}")
        if _safe_str(os.getenv('E2E_DIRECT_USER_ID')):
            print(f"[config] direct_user_id={os.getenv('E2E_DIRECT_USER_ID')}")
            print(f"[config] direct_org_id={os.getenv('E2E_DIRECT_ORG_ID')}")
    else:
        print("[config] gateway_mode=true")

    rounds: list[dict[str, Any]] = []
    cards_seen: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    last_pending: dict[str, Any] = {}
    dialogue_quality: dict[str, Any] | None = None
    document_quality: dict[str, Any] | None = None

    summary: dict[str, Any] = {
        "base_url": base_url,
        "username": username,
        "service_type_id": effective_service_type,
        "template_id": template_id,
        "template_name": "",
        "output_key": output_key,
        "strict_dialogue": bool(args.strict_dialogue),
        "strict_quality": bool(args.strict_quality),
        "session_id": "",
        "matter_id": "",
        "uploaded_file_ids": [],
        "evidence_files": [],
        "report_dir": str(out_dir),
        "status": "running",
        "started_at": datetime.now().isoformat(),
    }
    summary["stop_after_node"] = stop_after_node or None
    summary["debug_json"] = bool(args.debug_json)
    summary["node_timeline_path"] = str(out_dir / "node_timeline.json")
    summary["state_snapshots_dir"] = str(out_dir / "state_snapshots")

    state_snapshots_dir = out_dir / "state_snapshots"
    state_snapshots_dir.mkdir(parents=True, exist_ok=True)
    node_timeline: list[dict[str, Any]] = []
    docgen_node_sequence: list[str] = []
    last_docgen_snapshot: dict[str, Any] = {}
    flow: WorkbenchFlow | None = None
    observer: Any = None

    async def _record_round(
        *,
        action: str,
        payload: dict[str, Any],
        sse: dict[str, Any],
        enforce_visibility: bool,
    ) -> None:
        round_no = len(rounds) + 1
        busy = is_session_busy_sse(sse)
        output_text = _safe_str(sse.get("output"))
        event_count = _event_counts(sse)
        cards_in_sse = _extract_last_cards(sse)
        events_obj = sse.get("events")
        events: list[Any] = events_obj if isinstance(events_obj, list) else []
        has_stream_events = any(isinstance(evt, dict) for evt in events)

        visible_ok = True
        visible_error = ""
        if enforce_visibility and (not busy):
            try:
                assert_visible_response(sse)
            except Exception as e:  # noqa: BLE001
                visible_error = str(e)
                if _resume_ack_only_response(action=action, sse=sse, visible_error=visible_error):
                    visible_ok = True
                elif _kickoff_ack_only_response(action=action, sse=sse, visible_error=visible_error):
                    visible_ok = True
                else:
                    visible_ok = False

        prev_streak = int(rounds[-1].get("low_signal_streak") or 0) if rounds else 0
        low_signal_streak = 0
        resume_ack_only = (
            str(action or "").startswith("resume")
            and visible_ok
            and _sse_has_user_message_event(sse)
            and not cards_in_sse
        )
        kickoff_ack_only = (
            action == "chat.kickoff"
            and visible_ok
            and _sse_has_user_message_event(sse)
            and not cards_in_sse
        )
        if (
            (not busy)
            and has_stream_events
            and (not cards_in_sse)
            and (not resume_ack_only)
            and (not kickoff_ack_only)
            and _is_low_signal_output(output_text)
        ):
            low_signal_streak = prev_streak + 1

        row = {
            "round": round_no,
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "payload": payload,
            "busy": busy,
            "event_counts": event_count,
            "output_length": len(output_text),
            "output_preview": output_text[:220],
            "card_count": len(cards_in_sse),
            "visible_ok": visible_ok,
            "visible_error": visible_error,
            "low_signal_streak": low_signal_streak,
        }
        rounds.append(row)

        print(
            f"[round {round_no}] action={action} busy={busy} cards={len(cards_in_sse)} "
            f"output_len={len(output_text)} low_signal_streak={low_signal_streak}",
            flush=True,
        )

        for card in cards_in_sse:
            cards_seen.append(
                {
                    "source": "sse",
                    "round": round_no,
                    "skill_id": _safe_str(card.get("skill_id")),
                    "task_key": _safe_str(card.get("task_key")),
                    "review_type": _safe_str(card.get("review_type")),
                    "card": card,
                }
            )

        for idx, evt in enumerate(events):
            if not isinstance(evt, dict):
                continue
            event_rows.append(
                {
                    "round": round_no,
                    "action": action,
                    "event_index": idx,
                    "event": _safe_str(evt.get("event")) or "unknown",
                    "data": evt.get("data"),
                }
            )

        if bool(args.strict_dialogue) and enforce_visibility and (not busy):
            if not visible_ok:
                raise AssertionError(f"dialogue visible response check failed at round={round_no}: {visible_error}")
            if low_signal_streak > int(args.max_low_signal_streak):
                raise AssertionError(
                    f"dialogue stalled on low-signal responses at round={round_no}, "
                    f"streak={low_signal_streak}, threshold={args.max_low_signal_streak}"
                )

        if observer is not None:
            await observer(trigger=action)

    resume_busy_retries = max(1, int(os.getenv("E2E_RESUME_BUSY_RETRIES", "6") or 6))

    async def _resume_card_with_busy_retry(
        *,
        flow: WorkbenchFlow,
        card: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        attempt = 0
        last_sse: dict[str, Any] = {}
        while attempt < resume_busy_retries:
            attempt += 1
            last_sse = await flow.resume_card(card, max_loops=max(1, int(args.max_loops)))
            payload_with_attempt = dict(payload)
            payload_with_attempt["attempt"] = attempt
            await _record_round(
                action=action,
                payload=payload_with_attempt,
                sse=last_sse if isinstance(last_sse, dict) else {},
                enforce_visibility=True,
            )
            if _sse_has_user_message_event(last_sse if isinstance(last_sse, dict) else {}):
                return last_sse
            if not is_session_busy_sse(last_sse if isinstance(last_sse, dict) else {}):
                return last_sse
            await asyncio.sleep(min(2.5, 0.4 * attempt + 0.4))
        return last_sse

    async with ApiClient(base_url) as client:
        try:
            await client.login(username, password)
            print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

            async def _observe_runtime_state(
                *,
                trigger: str,
                pending_card: dict[str, Any] | None = None,
                deliverable_rows: list[dict[str, Any]] | None = None,
            ) -> dict[str, Any]:
                nonlocal last_docgen_snapshot, docgen_node_sequence
                matter_ref = _safe_str((flow.matter_id if flow is not None else "") or summary.get("matter_id"))
                session_ref = _safe_str(summary.get("session_id"))
                errors: dict[str, str] = {}

                session_data: dict[str, Any] = {}
                workbench_snapshot: dict[str, Any] = {}
                workflow_snapshot: dict[str, Any] = {}
                phase_timeline: dict[str, Any] = {}
                matter_timeline: dict[str, Any] = {}
                trace_rows: list[dict[str, Any]] = []
                pending_effective = pending_card if isinstance(pending_card, dict) else None
                deliverable_rows_effective = [row for row in (deliverable_rows or []) if isinstance(row, dict)]

                if session_ref:
                    try:
                        session_resp = await client.get_session(session_ref)
                        unwrapped = unwrap_api_response(session_resp)
                        session_data = unwrapped if isinstance(unwrapped, dict) else {}
                    except Exception as exc:  # noqa: BLE001
                        errors["session"] = str(exc)

                    if pending_effective is None:
                        try:
                            pending_resp = await client.get_pending_card(session_ref)
                            pending_unwrapped = unwrap_api_response(pending_resp)
                            pending_effective = pending_unwrapped if isinstance(pending_unwrapped, dict) and pending_unwrapped else None
                        except Exception as exc:  # noqa: BLE001
                            errors["pending_card"] = str(exc)

                    try:
                        traces_resp = await client.list_session_traces(session_ref, limit=40)
                        traces_data = unwrap_api_response(traces_resp)
                        rows = traces_data.get("traces") if isinstance(traces_data, dict) else None
                        trace_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
                    except Exception as exc:  # noqa: BLE001
                        errors["session_traces"] = str(exc)

                if matter_ref:
                    try:
                        wb_resp = await client.get(f"/matter-service/lawyer/matters/{matter_ref}/workbench/snapshot")
                        wb_data = unwrap_api_response(wb_resp)
                        workbench_snapshot = wb_data if isinstance(wb_data, dict) else {}
                    except Exception as exc:  # noqa: BLE001
                        errors["workbench_snapshot"] = str(exc)

                    try:
                        wf_resp = await client.get_workflow_snapshot(matter_ref)
                        wf_data = unwrap_api_response(wf_resp)
                        workflow_snapshot = wf_data if isinstance(wf_data, dict) else {}
                    except Exception as exc:  # noqa: BLE001
                        errors["workflow_snapshot"] = str(exc)

                    try:
                        pt_resp = await client.get_matter_phase_timeline(matter_ref)
                        pt_data = unwrap_api_response(pt_resp)
                        phase_timeline = pt_data if isinstance(pt_data, dict) else {}
                    except Exception as exc:  # noqa: BLE001
                        errors["phase_timeline"] = str(exc)

                    if not deliverable_rows_effective:
                        try:
                            deliverable_rows_effective = await _list_deliverables(client, matter_ref, output_key)
                        except Exception as exc:  # noqa: BLE001
                            errors["deliverables"] = str(exc)

                    if bool(args.debug_json):
                        try:
                            tl_resp = await client.get_matter_timeline(matter_ref, limit=40)
                            tl_data = unwrap_api_response(tl_resp)
                            matter_timeline = tl_data if isinstance(tl_data, dict) else {}
                        except Exception as exc:  # noqa: BLE001
                            errors["matter_timeline"] = str(exc)

                snapshot = _extract_docgen_snapshot(
                    matter_id=matter_ref,
                    session_id=session_ref,
                    workbench_snapshot=workbench_snapshot,
                    workflow_snapshot=workflow_snapshot,
                    phase_timeline=phase_timeline,
                    session=session_data,
                    pending_card=pending_effective,
                    deliverables=deliverable_rows_effective,
                    traces=trace_rows,
                )
                trace_obj_raw = snapshot.get("trace")
                trace_obj = trace_obj_raw if isinstance(trace_obj_raw, dict) else {}
                trace_node_ids_raw = trace_obj.get("trace_node_ids")
                trace_node_ids = trace_node_ids_raw if isinstance(trace_node_ids_raw, list) else []
                current_node = _detect_docgen_node(
                    current_task_id=_safe_str(snapshot.get("current_task_id")),
                    current_phase=_safe_str(snapshot.get("current_phase")),
                    pending_card=snapshot.get("pending_card") if isinstance(snapshot.get("pending_card"), dict) else {},
                    deliverable=snapshot.get("deliverable") if isinstance(snapshot.get("deliverable"), dict) else {},
                    docgen=snapshot.get("docgen") if isinstance(snapshot.get("docgen"), dict) else {},
                    trace_node_ids=trace_node_ids,
                    template_quality_contracts_json_exists=bool(snapshot.get("template_quality_contracts_json_exists")),
                    docgen_repair_plan_exists=bool(snapshot.get("docgen_repair_plan_exists")),
                    quality_review_decision=_safe_str(snapshot.get("quality_review_decision")),
                )
                docgen_node_sequence = _extend_docgen_node_sequence(
                    existing=docgen_node_sequence,
                    snapshot=snapshot,
                    current_node=current_node,
                )

                observed_at = datetime.now().isoformat()
                step_no = len(node_timeline) + 1
                snapshot["docgen_node"] = current_node
                snapshot["docgen_node_sequence"] = list(docgen_node_sequence)
                snapshot["observed_at"] = observed_at
                snapshot["trigger"] = trigger
                if errors:
                    snapshot["collection_errors"] = errors
                if bool(args.debug_json):
                    snapshot["raw"] = {
                        "session": session_data,
                        "workbench_snapshot": workbench_snapshot,
                        "workflow_snapshot": workflow_snapshot,
                        "phase_timeline": phase_timeline,
                        "matter_timeline": matter_timeline,
                        "pending_card": pending_effective or {},
                        "deliverables": deliverable_rows_effective,
                        "traces": trace_rows,
                    }

                node_timeline.append(
                    _build_node_timeline_row(
                        step=step_no,
                        trigger=trigger,
                        observed_at=observed_at,
                        docgen_snapshot=snapshot,
                        docgen_node_sequence=docgen_node_sequence,
                    )
                )
                _write_json(state_snapshots_dir / f"step_{step_no:03d}.json", snapshot)
                _write_json(out_dir / "node_timeline.json", node_timeline)

                deliverable_obj_raw = snapshot.get("deliverable")
                deliverable_obj = deliverable_obj_raw if isinstance(deliverable_obj_raw, dict) else {}
                summary.update(
                    {
                        "docgen_node_sequence": list(docgen_node_sequence),
                        "latest_docgen_node": current_node,
                        "deliverable_status": _safe_str(deliverable_obj.get("status")),
                        "template_quality_contracts_json_exists": bool(snapshot.get("template_quality_contracts_json_exists")),
                        "docgen_repair_plan_exists": bool(snapshot.get("docgen_repair_plan_exists")),
                        "docgen_repair_contracts_json_exists": bool(snapshot.get("docgen_repair_contracts_json_exists")),
                        "quality_review_decision": _safe_str(snapshot.get("quality_review_decision")),
                        "soft_reason_codes": snapshot.get("soft_reason_codes") if isinstance(snapshot.get("soft_reason_codes"), list) else [],
                    }
                )
                last_docgen_snapshot = snapshot

                if stop_after_node and _is_stop_node_reached(
                    target_node=stop_after_node,
                    current_node=current_node,
                    seen_nodes=docgen_node_sequence,
                ):
                    summary.update(
                        {
                            "status": "stopped_after_node",
                            "stop_after_node": stop_after_node,
                            "stop_reached_node": current_node,
                            "stop_reached_at": observed_at,
                            "stop_reached_step": step_no,
                        }
                    )
                    raise StopAfterNodeReached(snapshot, stop_after_node)

                return snapshot

            observer = _observe_runtime_state

            evidence_paths = _resolve_evidence_paths(args.evidence_file)
            summary["evidence_files"] = [str(p) for p in evidence_paths]
            uploaded_file_ids: list[str] = []
            for p in evidence_paths:
                upload = await client.upload_file(str(p), purpose="consultation")
                file_id = _safe_str(((upload.get("data") or {}) if isinstance(upload, dict) else {}).get("id"))
                if not file_id:
                    raise RuntimeError(f"upload_file failed for {p}: {upload}")
                uploaded_file_ids.append(file_id)
                print(f"[upload] {p.name} -> file_id={file_id}")
            summary["uploaded_file_ids"] = uploaded_file_ids

            try:
                matter = await _create_matter_with_retry(
                    client,
                    service_type_id=effective_service_type,
                    title=(
                        f"E2E 智能模板起草（service_type={effective_service_type}, "
                        f"template_id={template_id}, run={run_token}）"
                    ),
                    file_ids=uploaded_file_ids,
                    max_attempts=max(3, int(os.getenv("E2E_CREATE_MATTER_ATTEMPTS", "6") or 6)),
                )
            except Exception as e:
                print(json.dumps({"stage": "create_matter", **_http_error_details(e)}, ensure_ascii=False), flush=True)
                raise
            matter_id = _safe_str(((matter.get("data") or {}) if isinstance(matter, dict) else {}).get("id"))
            if not matter_id:
                raise RuntimeError(f"create_matter failed: {matter}")

            sess = await _create_session_with_retry(
                client,
                matter_id,
                max_attempts=max(3, int(os.getenv("E2E_CREATE_SESSION_RETRIES", "6") or 6)),
            )
            session_id = _safe_str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id"))
            if not session_id:
                raise RuntimeError(f"create_session failed: {sess}")

            summary["session_id"] = session_id
            summary["matter_id"] = matter_id
            print(f"[session] id={session_id} matter_id={matter_id}")

            template_name = await _resolve_template_name(client, template_id, _safe_str(args.template_name))
            summary["template_name"] = template_name
            print(f"[template] id={template_id} name={template_name}")

            start_sse = await client.workflow_action(
                session_id,
                workflow_action="template_draft_start",
                workflow_action_params={
                    "template_id": template_id,
                    "deliverable_title": template_name,
                    "output_key": output_key,
                    "template_key": output_key,
                },
                settle_mode="fire_and_poll",
            )
            await _record_round(
                action="workflow_action.template_draft_start",
                payload={"template_id": template_id, "output_key": output_key, "template_key": output_key},
                sse=start_sse if isinstance(start_sse, dict) else {},
                enforce_visibility=False,
            )
            start_errors = _sse_error_events(start_sse if isinstance(start_sse, dict) else {})
            if start_errors:
                if _workflow_action_ack_only_response(
                    action="workflow_action.template_draft_start",
                    sse=start_sse if isinstance(start_sse, dict) else {},
                ):
                    print(
                        json.dumps(
                            {
                                "stage": "workflow_action.template_draft_start",
                                "errors": start_errors,
                                "ack_only": True,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    await asyncio.sleep(2.0)
                else:
                    print(json.dumps({"stage": "workflow_action.template_draft_start", "errors": start_errors}, ensure_ascii=False), flush=True)
                    raise RuntimeError(f"template_draft_start returned error events: {start_errors}")

            flow_overrides = _build_flow_overrides(
                facts_text,
                uploaded_file_ids,
                service_type_id=effective_service_type,
                template_name=template_name,
            )
            flow = WorkbenchFlow(
                client=client,
                session_id=session_id,
                uploaded_file_ids=uploaded_file_ids,
                overrides=flow_overrides,
                strict_card_driven=True,
                matter_id=matter_id,
            )
            start_settle = await _wait_template_draft_start_settled(
                client,
                session_id=session_id,
                matter_id=matter_id,
                timeout_s=max(8.0, min(30.0, poll_interval_s * 6)),
                poll_interval_s=poll_interval_s,
            )
            start_pending_card = start_settle.get("pending_card") if isinstance(start_settle.get("pending_card"), dict) else {}

            kickoff_sse: dict[str, Any] = {}
            if start_pending_card:
                await _record_round(
                    action="chat.kickoff.skipped_existing_card",
                    payload={
                        "attachments": len(uploaded_file_ids),
                        "settle_reason": _safe_str(start_settle.get("reason")),
                    },
                    sse={},
                    enforce_visibility=False,
                )
            else:
                kickoff_sse = await flow.nudge(
                    facts_text,
                    attachments=uploaded_file_ids,
                    max_loops=max(1, int(args.max_loops)),
                    settle_mode="fire_and_poll",
                )
                await _record_round(
                    action="chat.kickoff",
                    payload={"attachments": len(uploaded_file_ids), "settle_reason": _safe_str(start_settle.get("reason"))},
                    sse=kickoff_sse if isinstance(kickoff_sse, dict) else {},
                    enforce_visibility=True,
                )
                kickoff_output = _safe_str((kickoff_sse if isinstance(kickoff_sse, dict) else {}).get("output"))
                if _is_terminal_failure_output(kickoff_output):
                    raise RuntimeError(f"kickoff returned terminal failure output: {kickoff_output}")

            kickoff_card = extract_last_card_from_sse(kickoff_sse if isinstance(kickoff_sse, dict) else {})
            resume_kickoff_sse_card = str(os.getenv("E2E_RESUME_KICKOFF_SSE_CARD", "0") or "0").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if isinstance(kickoff_card, dict) and kickoff_card:
                cards_seen.append(
                    {
                        "source": "sse",
                        "round": len(rounds) + 1,
                        "skill_id": _safe_str(kickoff_card.get("skill_id")),
                        "task_key": _safe_str(kickoff_card.get("task_key")),
                        "review_type": _safe_str(kickoff_card.get("review_type")),
                        "card": kickoff_card,
                    }
                )
                # Default hard-cut: rely on pending_card API in the main loop as the single resume source.
                # Resuming the kickoff SSE card immediately can race with backend card persistence and
                # produce stale-card retries / websocket stalls in remote environments.
                if resume_kickoff_sse_card:
                    await _resume_card_with_busy_retry(
                        flow=flow,
                        card=kickoff_card,
                        action="resume.kickoff_card",
                        payload={
                            "skill_id": _safe_str(kickoff_card.get("skill_id")),
                            "task_key": _safe_str(kickoff_card.get("task_key")),
                        },
                    )

            busy_retries = 0
            busy_hold_until = 0.0
            busy_nudge_hold_s = max(0.0, float(os.getenv("E2E_BUSY_NUDGE_HOLD_S", "240") or 240))
            suppress_nudge_rounds = 0
            last_card_sig = ""
            last_card_repeats = 0
            deliverable_row: dict[str, Any] | None = None
            last_deliverable_sig = ""
            stall_rounds = 0
            cause_anchor_uploaded = False
            max_steps = max(1, int(args.max_steps))
            max_same_card_repeats = max(1, int(args.max_same_card_repeats))
            max_skill_error_repeats = max(1, int(args.max_skill_error_repeats))
            max_stall_rounds = max(1, int(args.max_stall_rounds))
            cause_anchor_repeat_threshold = max(1, int(args.cause_anchor_repeat_threshold))
            deliverable_settle_grace_s = max(0.0, float(os.getenv("E2E_DELIVERABLE_SETTLE_GRACE_S", "45") or 45))
            deliverable_min_stable_polls = max(1, int(os.getenv("E2E_DELIVERABLE_MIN_STABLE_POLLS", "2") or 2))
            use_pending_card_api = str(os.getenv("E2E_USE_PENDING_CARD_API", "1") or "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            deliverable_candidate_seen_at = 0.0
            deliverable_candidate_sig = ""
            deliverable_candidate_stable_polls = 0
            for step_idx in range(max_steps):
                if step_idx > 0 and poll_interval_s > 0:
                    await asyncio.sleep(poll_interval_s)
                await flow.refresh()
                deliverable_head: dict[str, Any] | None = None
                deliverable_rows: list[dict[str, Any]] = []
                deliverable_candidate: dict[str, Any] | None = None
                if flow.matter_id:
                    deliverable_rows = await _list_deliverables(client, flow.matter_id, output_key)
                    if deliverable_rows:
                        deliverable_head = deliverable_rows[0]
                        deliverable_candidate = _pick_deliverable_with_file(deliverable_rows)
                    head_sig = _deliverable_signature(deliverable_head)
                    if head_sig and head_sig != last_deliverable_sig:
                        last_deliverable_sig = head_sig
                        stall_rounds = 0
                if observer is not None:
                    await observer(trigger="poll.loop", deliverable_rows=deliverable_rows)

                if deliverable_candidate:
                    candidate_sig = _deliverable_signature(deliverable_candidate)
                    now = asyncio.get_running_loop().time()
                    if candidate_sig and candidate_sig != deliverable_candidate_sig:
                        deliverable_candidate_sig = candidate_sig
                        deliverable_candidate_seen_at = now
                        deliverable_candidate_stable_polls = 1
                    elif candidate_sig:
                        deliverable_candidate_stable_polls += 1
                    if _deliverable_candidate_settled(
                        snapshot=last_docgen_snapshot,
                        stable_polls=deliverable_candidate_stable_polls,
                        seen_for_s=(now - deliverable_candidate_seen_at) if deliverable_candidate_seen_at > 0 else 0.0,
                        min_stable_polls=deliverable_min_stable_polls,
                        settle_grace_s=deliverable_settle_grace_s,
                    ):
                        deliverable_row = deliverable_candidate
                        break

                pending = await flow.get_pending_card() if use_pending_card_api else None
                if observer is not None and pending:
                    await observer(trigger="poll.pending_card", pending_card=pending, deliverable_rows=deliverable_rows)
                if pending:
                    stall_rounds = 0
                    skill_id = _safe_str(pending.get("skill_id"))
                    task_key = _safe_str(pending.get("task_key"))
                    prompt_preview = _safe_str(pending.get("prompt"))[:220]
                    card_sig = f"{skill_id}|{task_key}"
                    now = asyncio.get_running_loop().time()
                    if card_sig and card_sig == last_card_sig and busy_hold_until > now:
                        await asyncio.sleep(min(2.5, max(0.3, busy_hold_until - now)))
                        continue
                    if card_sig and card_sig == last_card_sig:
                        last_card_repeats += 1
                    else:
                        last_card_sig = card_sig
                        last_card_repeats = 1

                    if last_card_repeats >= max_same_card_repeats:
                        raise AssertionError(
                            "workflow stuck on repeated pending card: "
                            f"skill_id={skill_id}, task_key={task_key}, repeats={last_card_repeats}, "
                            f"prompt={prompt_preview}"
                        )

                    if (
                        (not cause_anchor_uploaded)
                        and cause_anchor_path is not None
                        and skill_id == "cause-recommendation"
                        and task_key == "cause_disambiguation"
                        and last_card_repeats >= cause_anchor_repeat_threshold
                    ):
                        upload = await client.upload_file(str(cause_anchor_path), purpose="consultation")
                        anchor_file_id = _safe_str(
                            ((upload.get("data") or {}) if isinstance(upload, dict) else {}).get("id")
                        )
                        if not anchor_file_id:
                            raise RuntimeError(f"cause anchor upload failed: {upload}")
                        if anchor_file_id not in uploaded_file_ids:
                            uploaded_file_ids.append(anchor_file_id)
                        flow.uploaded_file_ids = uploaded_file_ids
                        flow.overrides["attachment_file_ids"] = [
                            str(x).strip() for x in uploaded_file_ids if _safe_str(x)
                        ]
                        summary["uploaded_file_ids"] = list(uploaded_file_ids)
                        summary["cause_anchor_file"] = str(cause_anchor_path)
                        summary["cause_anchor_file_id"] = anchor_file_id
                        cause_anchor_uploaded = True
                        print(
                            f"[remediation] cause anchor uploaded: {cause_anchor_path.name} -> file_id={anchor_file_id}",
                            flush=True,
                        )

                    if skill_id == "skill-error-analysis" and last_card_repeats >= max_skill_error_repeats:
                        raise AssertionError(
                            "document generation blocked by repeated skill-error-analysis card: "
                            f"repeats={last_card_repeats}, prompt={prompt_preview}"
                        )

                    cards_seen.append(
                        {
                            "source": "pending_card",
                            "round": len(rounds) + 1,
                            "skill_id": skill_id,
                            "task_key": task_key,
                            "review_type": _safe_str(pending.get("review_type")),
                            "card": pending,
                        }
                    )
                    sse = await _resume_card_with_busy_retry(
                        flow=flow,
                        card=pending,
                        action="resume.card",
                        payload={
                            "skill_id": skill_id,
                            "task_key": task_key,
                        },
                    )

                else:
                    last_card_sig = ""
                    last_card_repeats = 0
                    now = asyncio.get_running_loop().time()
                    if deliverable_candidate:
                        await _record_round(
                            action="state.wait_deliverable_settle",
                            payload={
                                "deliverable_status": _safe_str((deliverable_candidate or {}).get("status")),
                                "deliverable_file_id": _safe_str((deliverable_candidate or {}).get("file_id")),
                                "settle_seen_for_s": round((now - deliverable_candidate_seen_at), 2) if deliverable_candidate_seen_at > 0 else 0.0,
                                "stable_polls": deliverable_candidate_stable_polls,
                                "settle_grace_s": deliverable_settle_grace_s,
                                "current_phase": _safe_str(last_docgen_snapshot.get("current_phase")),
                                "current_task_id": _safe_str(last_docgen_snapshot.get("current_task_id")),
                                "docgen_node": _safe_str(last_docgen_snapshot.get("docgen_node")),
                                "quality_review_decision": _safe_str(last_docgen_snapshot.get("quality_review_decision")),
                            },
                            sse={},
                            enforce_visibility=False,
                        )
                        stall_rounds = 0
                        await asyncio.sleep(max(0.5, float(args.poll_interval_s)))
                        continue

                    if deliverable_head:
                        current_sig = _deliverable_signature(deliverable_head)
                        if (not current_sig) or current_sig == last_deliverable_sig:
                            stall_rounds += 1
                        else:
                            last_deliverable_sig = current_sig
                            stall_rounds = 0
                    else:
                        stall_rounds += 1

                    if stall_rounds >= max_stall_rounds and busy_hold_until <= now:
                        raise AssertionError(
                            "workflow stalled with no pending card and no deliverable progress: "
                            f"stall_rounds={stall_rounds}, deliverable_status={_safe_str((deliverable_head or {}).get('status'))}"
                        )

                    if suppress_nudge_rounds > 0:
                        suppress_nudge_rounds -= 1
                        await asyncio.sleep(min(2.5, 0.3 * max(1, busy_retries) + 0.5))
                        continue

                    if busy_hold_until > now:
                        stall_rounds = 0
                        await asyncio.sleep(min(2.5, max(0.3, busy_hold_until - now)))
                        continue

                    await _record_round(
                        action="state.wait_no_card",
                        payload={
                            "stall_rounds": stall_rounds,
                            "deliverable_status": _safe_str((deliverable_head or {}).get("status")),
                            "current_phase": _safe_str(last_docgen_snapshot.get("current_phase")),
                            "current_task_id": _safe_str(last_docgen_snapshot.get("current_task_id")),
                            "docgen_node": _safe_str(last_docgen_snapshot.get("docgen_node")),
                        },
                        sse={},
                        enforce_visibility=False,
                    )
                    await asyncio.sleep(max(0.5, float(args.poll_interval_s)))
                    continue

                if is_session_busy_sse(sse if isinstance(sse, dict) else {}):
                    busy_retries += 1
                    if busy_nudge_hold_s > 0:
                        busy_hold_until = max(busy_hold_until, asyncio.get_running_loop().time() + busy_nudge_hold_s)
                    suppress_nudge_rounds = min(24, max(suppress_nudge_rounds, 2 + busy_retries // 2))
                    await asyncio.sleep(min(2.5, 0.2 * busy_retries + 0.5))
                else:
                    busy_retries = 0
                    busy_hold_until = 0.0
                    suppress_nudge_rounds = 0

            if not flow.matter_id:
                raise RuntimeError("matter_id missing after workflow loop")
            if not deliverable_row:
                deliverable_row = await _first_deliverable_with_file(client, flow.matter_id, output_key)
            if not deliverable_row:
                raise AssertionError(f"deliverable not ready after max_steps={max_steps}, output_key={output_key}")

            pending_after = await flow.get_pending_card()
            if pending_after and _safe_str(pending_after.get("skill_id")) == "document-generation":
                cards_seen.append(
                    {
                        "source": "pending_card",
                        "round": len(rounds) + 1,
                        "skill_id": _safe_str(pending_after.get("skill_id")),
                        "task_key": _safe_str(pending_after.get("task_key")),
                        "review_type": _safe_str(pending_after.get("review_type")),
                        "card": pending_after,
                    }
                )
                confirm_sse = await _resume_card_with_busy_retry(
                    flow=flow,
                    card=pending_after,
                    action="resume.confirm_card",
                    payload={
                        "skill_id": _safe_str(pending_after.get("skill_id")),
                        "task_key": _safe_str(pending_after.get("task_key")),
                    },
                )

            async def _deliverable_terminal() -> bool:
                rows = await _list_deliverables(client, flow.matter_id or matter_id, output_key)
                if not rows:
                    return False
                return _safe_str(rows[0].get("status")).lower() in {"archived", "completed", "done"}

            await eventually(
                _deliverable_terminal,
                timeout_s=120,
                interval_s=3,
                description="deliverable terminal",
            )

            rows = await _list_deliverables(client, flow.matter_id or matter_id, output_key)
            if observer is not None:
                await observer(trigger="deliverable.terminal", deliverable_rows=rows)
            if not rows:
                raise AssertionError("no deliverables after archive wait")
            deliverable = rows[0]
            file_id = _safe_str(deliverable.get("file_id"))
            status = _safe_str(deliverable.get("status"))
            if not file_id:
                raise AssertionError(f"deliverable has no file_id: {deliverable}")

            docx_bytes = await client.download_file_bytes(file_id)
            docx_text = extract_docx_text(docx_bytes)
            (out_dir / "document.docx").write_bytes(docx_bytes)
            (out_dir / "document.txt").write_text(docx_text, encoding="utf-8")

            dialogue_quality = _evaluate_dialogue_quality(
                rounds=rounds,
                cards=cards_seen,
                strict_dialogue=bool(args.strict_dialogue),
            )
            document_quality = _evaluate_document_quality(
                text=docx_text,
                targets=doc_targets,
                min_citations=max(0, int(args.min_citations)),
                deliverable_status=status,
                strict_quality=bool(args.strict_quality),
            )

            (out_dir / "dialogue_quality.json").write_text(
                json.dumps(dialogue_quality, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "document_quality.json").write_text(
                json.dumps(document_quality, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if bool(args.strict_dialogue) and not bool(dialogue_quality.get("pass")):
                raise AssertionError("dialogue quality gate failed")
            if bool(args.strict_quality) and not bool(document_quality.get("pass")):
                raise AssertionError("document quality gate failed")

            summary.update(
                {
                    "status": "passed",
                    "ended_at": datetime.now().isoformat(),
                    "deliverable": {
                        "id": deliverable.get("id"),
                        "file_id": file_id,
                        "status": status,
                        "output_key": _safe_str(deliverable.get("output_key")),
                        "title": _safe_str(deliverable.get("title")),
                    },
                    "dialogue_quality_pass": bool(dialogue_quality.get("pass")),
                    "document_quality_pass": bool(document_quality.get("pass")),
                    "round_count": len(rounds),
                    "docgen_node_sequence": list(docgen_node_sequence),
                    "latest_docgen_node": _safe_str(last_docgen_snapshot.get("docgen_node")),
                    "template_quality_contracts_json_exists": bool(last_docgen_snapshot.get("template_quality_contracts_json_exists")),
                    "docgen_repair_plan_exists": bool(last_docgen_snapshot.get("docgen_repair_plan_exists")),
                    "docgen_repair_contracts_json_exists": bool(last_docgen_snapshot.get("docgen_repair_contracts_json_exists")),
                    "quality_review_decision": _safe_str(last_docgen_snapshot.get("quality_review_decision")),
                    "soft_reason_codes": last_docgen_snapshot.get("soft_reason_codes") if isinstance(last_docgen_snapshot.get("soft_reason_codes"), list) else [],
                    "deliverable_status": status,
                }
            )
            bundle_export = export_observability_bundle(
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
                reason="template_draft_passed",
            )
            bundle_quality = build_bundle_quality_reports(
                repo_root=REPO_ROOT,
                bundle_dir=bundle_export["bundle_dir"],
                flow_id="template_draft",
                snapshot={},
                current_view={},
                goal_completion_mode="none",
            )
            flow_scores = build_template_flow_scores(
                cards=cards_seen,
                pending_card=last_pending if isinstance(last_pending, dict) else {},
                node_timeline=node_timeline,
                summary=summary,
                last_docgen_snapshot=last_docgen_snapshot,
                dialogue_quality=dialogue_quality,
                document_quality=document_quality,
                bundle_quality_summary=bundle_quality,
            )
            summary["flow_scores"] = flow_scores
            summary["bundle_quality"] = bundle_quality
            summary["debug_refs"] = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
            )
            quality_summary_ref = str((bundle_quality.get("refs") or {}).get("summary") or "").strip()
            if quality_summary_ref:
                bundle_refs = summary["debug_refs"].get("bundle_refs") if isinstance(summary.get("debug_refs"), dict) and isinstance(summary["debug_refs"].get("bundle_refs"), list) else []
                if quality_summary_ref not in bundle_refs:
                    bundle_refs.append(quality_summary_ref)
                    summary["debug_refs"]["bundle_refs"] = bundle_refs

            _write_json(out_dir / "deliverables.json", {"deliverables": rows})
            _write_json(out_dir / "cards.json", cards_seen)
            _write_events_ndjson(out_dir / "events.ndjson", event_rows)
            _write_json(out_dir / "node_timeline.json", node_timeline)
            _write_json(out_dir / "bundle_quality.json", bundle_quality)
            _write_json(out_dir / "flow_scores.json", flow_scores)
            _write_json(out_dir / "debug_refs.json", summary.get("debug_refs") if isinstance(summary.get("debug_refs"), dict) else {})
            _write_json(out_dir / "summary.json", summary)

            print("[done] template draft workflow completed")
            print(f"[artifacts] {out_dir}")
            return 0

        except StopAfterNodeReached as stop_exc:
            if dialogue_quality is None:
                dialogue_quality = _evaluate_dialogue_quality(
                    rounds=rounds,
                    cards=cards_seen,
                    strict_dialogue=bool(args.strict_dialogue),
                )

            matter_ref = _safe_str(summary.get("matter_id"))
            stop_rows: list[dict[str, Any]] = []
            stop_doc_text = ""
            stop_deliverable_status = _safe_str(((stop_exc.snapshot.get("deliverable") or {}) if isinstance(stop_exc.snapshot.get("deliverable"), dict) else {}).get("status"))
            if matter_ref:
                try:
                    stop_rows = await _list_deliverables(client, matter_ref, output_key)
                    _write_json(out_dir / "deliverables.json", {"deliverables": stop_rows})
                except Exception:
                    stop_rows = []
            stop_candidate = _pick_deliverable_with_file(stop_rows)
            if stop_candidate:
                try:
                    stop_file_id = _safe_str(stop_candidate.get("file_id"))
                    if stop_file_id:
                        docx_bytes = await client.download_file_bytes(stop_file_id)
                        stop_doc_text = extract_docx_text(docx_bytes)
                        (out_dir / "document.docx").write_bytes(docx_bytes)
                        (out_dir / "document.txt").write_text(stop_doc_text, encoding="utf-8")
                        stop_deliverable_status = _safe_str(stop_candidate.get("status"))
                except Exception:
                    pass

            if document_quality is None:
                if stop_doc_text:
                    document_quality = _evaluate_document_quality(
                        text=stop_doc_text,
                        targets=doc_targets,
                        min_citations=max(0, int(args.min_citations)),
                        deliverable_status=stop_deliverable_status,
                        strict_quality=bool(args.strict_quality),
                    )
                else:
                    document_quality = {
                        "strict_quality": bool(args.strict_quality),
                        "pass": False,
                        "failure_reasons": [f"stop_after_node={stop_exc.target_node} reached before最终文书下载"],
                        "placeholder_leak": None,
                        "citation_count": 0,
                        "citation_threshold": int(max(0, int(args.min_citations))),
                        "fact_coverage_score": 0.0,
                        "party_expected": doc_targets.get("parties") or [],
                        "party_missing": doc_targets.get("parties") or [],
                        "amount_expected": doc_targets.get("amounts") or [],
                        "amount_missing": doc_targets.get("amounts") or [],
                        "claim_keywords_expected": doc_targets.get("claim_keywords") or [],
                        "claim_keywords_hit": [],
                        "deliverable_status": stop_deliverable_status,
                        "document_length": len(stop_doc_text),
                    }

            summary.update(
                {
                    "status": "stopped_after_node",
                    "ended_at": datetime.now().isoformat(),
                    "round_count": len(rounds),
                    "dialogue_quality_pass": bool(dialogue_quality.get("pass")),
                    "document_quality_pass": bool(document_quality.get("pass")),
                    "stop_after_node": stop_exc.target_node,
                    "stop_reached_node": _safe_str(stop_exc.snapshot.get("docgen_node")),
                    "stop_reached_step": len(node_timeline),
                    "docgen_node_sequence": list(docgen_node_sequence),
                    "latest_docgen_node": _safe_str(stop_exc.snapshot.get("docgen_node")),
                    "template_quality_contracts_json_exists": bool(stop_exc.snapshot.get("template_quality_contracts_json_exists")),
                    "docgen_repair_plan_exists": bool(stop_exc.snapshot.get("docgen_repair_plan_exists")),
                    "docgen_repair_contracts_json_exists": bool(stop_exc.snapshot.get("docgen_repair_contracts_json_exists")),
                    "quality_review_decision": _safe_str(stop_exc.snapshot.get("quality_review_decision")),
                    "soft_reason_codes": stop_exc.snapshot.get("soft_reason_codes") if isinstance(stop_exc.snapshot.get("soft_reason_codes"), list) else [],
                    "deliverable_status": stop_deliverable_status,
                }
            )
            bundle_export = export_observability_bundle(
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
                reason="template_draft_stopped_after_node",
            )
            bundle_quality = build_bundle_quality_reports(
                repo_root=REPO_ROOT,
                bundle_dir=bundle_export["bundle_dir"],
                flow_id="template_draft",
                snapshot={},
                current_view={},
                goal_completion_mode="none",
            )
            flow_scores = build_template_flow_scores(
                cards=cards_seen,
                pending_card=last_pending if isinstance(last_pending, dict) else {},
                node_timeline=node_timeline,
                summary=summary,
                last_docgen_snapshot=stop_exc.snapshot if isinstance(stop_exc.snapshot, dict) else last_docgen_snapshot,
                dialogue_quality=dialogue_quality,
                document_quality=document_quality,
                bundle_quality_summary=bundle_quality,
            )
            summary["flow_scores"] = flow_scores
            summary["bundle_quality"] = bundle_quality
            summary["debug_refs"] = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
            )
            quality_summary_ref = str((bundle_quality.get("refs") or {}).get("summary") or "").strip()
            if quality_summary_ref:
                bundle_refs = summary["debug_refs"].get("bundle_refs") if isinstance(summary.get("debug_refs"), dict) and isinstance(summary["debug_refs"].get("bundle_refs"), list) else []
                if quality_summary_ref not in bundle_refs:
                    bundle_refs.append(quality_summary_ref)
                    summary["debug_refs"]["bundle_refs"] = bundle_refs

            _write_events_ndjson(out_dir / "events.ndjson", event_rows)
            _write_json(out_dir / "cards.json", cards_seen)
            _write_json(out_dir / "dialogue_quality.json", dialogue_quality)
            _write_json(out_dir / "document_quality.json", document_quality)
            _write_json(out_dir / "node_timeline.json", node_timeline)
            _write_json(out_dir / "bundle_quality.json", bundle_quality)
            _write_json(out_dir / "flow_scores.json", flow_scores)
            _write_json(out_dir / "debug_refs.json", summary.get("debug_refs") if isinstance(summary.get("debug_refs"), dict) else {})
            _write_json(out_dir / "summary.json", summary)

            print(f"[stopped] stop_after_node={stop_exc.target_node} reached")
            print(f"[artifacts] {out_dir}")
            return 0

        except Exception as e:  # noqa: BLE001
            if dialogue_quality is None:
                dialogue_quality = _evaluate_dialogue_quality(
                    rounds=rounds,
                    cards=cards_seen,
                    strict_dialogue=bool(args.strict_dialogue),
                )
            if document_quality is None:
                document_quality = {
                    "strict_quality": bool(args.strict_quality),
                    "pass": False,
                    "failure_reasons": ["流程在文书下载/质量校验前失败，未形成可验收文书质量结果"],
                    "placeholder_leak": None,
                    "citation_count": 0,
                    "citation_threshold": int(max(0, int(args.min_citations))),
                    "fact_coverage_score": 0.0,
                    "party_expected": doc_targets.get("parties") or [],
                    "party_missing": doc_targets.get("parties") or [],
                    "amount_expected": doc_targets.get("amounts") or [],
                    "amount_missing": doc_targets.get("amounts") or [],
                    "claim_keywords_expected": doc_targets.get("claim_keywords") or [],
                    "claim_keywords_hit": [],
                    "deliverable_status": "",
                    "document_length": 0,
                }

            summary.update(
                {
                    "status": "failed",
                    "ended_at": datetime.now().isoformat(),
                    "error": str(e),
                    "round_count": len(rounds),
                    "dialogue_quality_pass": bool(dialogue_quality.get("pass")),
                    "document_quality_pass": bool(document_quality.get("pass")),
                    "docgen_node_sequence": list(docgen_node_sequence),
                    "latest_docgen_node": _safe_str(last_docgen_snapshot.get("docgen_node")),
                }
            )
            bundle_export = export_observability_bundle(
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
                reason="template_draft_failed",
            )
            bundle_quality = build_bundle_quality_reports(
                repo_root=REPO_ROOT,
                bundle_dir=bundle_export["bundle_dir"],
                flow_id="template_draft",
                snapshot={},
                current_view={},
                goal_completion_mode="none",
            )
            flow_scores = build_template_flow_scores(
                cards=cards_seen,
                pending_card=last_pending if isinstance(last_pending, dict) else {},
                node_timeline=node_timeline,
                summary=summary,
                last_docgen_snapshot=last_docgen_snapshot,
                dialogue_quality=dialogue_quality,
                document_quality=document_quality,
                bundle_quality_summary=bundle_quality,
            )
            summary["flow_scores"] = flow_scores
            summary["bundle_quality"] = bundle_quality
            summary["debug_refs"] = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=_safe_str(summary.get("session_id")),
                matter_id=_safe_str(summary.get("matter_id")),
            )
            quality_summary_ref = str((bundle_quality.get("refs") or {}).get("summary") or "").strip()
            if quality_summary_ref:
                bundle_refs = summary["debug_refs"].get("bundle_refs") if isinstance(summary.get("debug_refs"), dict) and isinstance(summary["debug_refs"].get("bundle_refs"), list) else []
                if quality_summary_ref not in bundle_refs:
                    bundle_refs.append(quality_summary_ref)
                    summary["debug_refs"]["bundle_refs"] = bundle_refs

            matter_id = _safe_str(summary.get("matter_id"))
            session_id = _safe_str(summary.get("session_id"))

            failure_diag: dict[str, Any] = {
                "error": str(e),
                "summary": summary,
                "rounds_tail": rounds[-10:],
                "cards_tail": cards_seen[-10:],
                "node_timeline_tail": node_timeline[-12:],
                "last_docgen_snapshot": last_docgen_snapshot,
            }

            if matter_id:
                try:
                    rows = await _list_deliverables(client, matter_id, output_key)
                    failure_diag["deliverables"] = rows
                    (out_dir / "deliverables.failure.json").write_text(
                        json.dumps({"deliverables": rows}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            if session_id:
                try:
                    sess = await client.get_session(session_id)
                    failure_diag["session"] = unwrap_api_response(sess)
                except Exception:
                    pass

            _write_events_ndjson(out_dir / "events.ndjson", event_rows)
            _write_json(out_dir / "cards.json", cards_seen)
            _write_json(out_dir / "dialogue_quality.json", dialogue_quality)
            _write_json(out_dir / "document_quality.json", document_quality)
            _write_json(out_dir / "node_timeline.json", node_timeline)
            _write_json(out_dir / "failure_diagnostics.json", failure_diag)
            _write_json(out_dir / "bundle_quality.json", bundle_quality)
            _write_json(out_dir / "flow_scores.json", flow_scores)
            _write_json(out_dir / "debug_refs.json", summary.get("debug_refs") if isinstance(summary.get("debug_refs"), dict) else {})
            _write_json(out_dir / "summary.json", summary)
            print(f"[failed] {e}")
            print(f"[artifacts] {out_dir}")
            return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run smart-template drafting workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct service URLs")
    parser.add_argument("--direct-local", action="store_true", help="Deprecated alias for direct service mode")
    parser.add_argument("--consultations-base-url", default="", help="Direct consultations-service base URL")
    parser.add_argument("--files-base-url", default="", help="Direct files-service base URL")
    parser.add_argument("--matter-base-url", default="", help="Direct matter-service base URL")
    parser.add_argument("--templates-base-url", default="", help="Direct templates-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id (skip auth only when set)")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id (skip auth only when set)")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--service-type-id", default=DEFAULT_SERVICE_TYPE_ID, help="Matter service_type_id")
    parser.add_argument("--template-id", required=True, help="Template ID used by template_draft_start")
    parser.add_argument("--template-name", default="", help="Optional override for deliverable title")
    parser.add_argument("--output-key", default="", help="Deliverable output_key; default template:<template_id>")
    parser.add_argument("--facts-file", default="", help="UTF-8 text file for kickoff facts")
    parser.add_argument(
        "--evidence-file",
        action="append",
        default=[],
        help="Additional evidence file path; can be passed multiple times",
    )
    parser.add_argument("--max-steps", type=int, default=160, help="Workflow driving max steps")
    parser.add_argument("--max-loops", type=int, default=12, help="WS max_loops per call")
    parser.add_argument("--poll-interval-s", type=float, default=2.0, help="Polling interval between state snapshots")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Kickoff/start once, then only poll and answer cards")
    parser.add_argument("--stop-after-node", default="", help=f"Stop successfully after node reached: {', '.join(DOCGEN_STOP_NODES)}")
    parser.add_argument("--debug-json", action="store_true", help="Include raw API payloads in state snapshots")
    parser.add_argument("--max-low-signal-streak", type=int, default=4, help="Dialogue low-signal streak threshold")
    parser.add_argument(
        "--max-same-card-repeats",
        type=int,
        default=24,
        help="Abort when the same pending card (skill+task) repeats too many times",
    )
    parser.add_argument(
        "--max-skill-error-repeats",
        type=int,
        default=10,
        help="Abort when skill-error-analysis card repeats too many times",
    )
    parser.add_argument(
        "--max-stall-rounds",
        type=int,
        default=36,
        help="Abort when no pending card and deliverable state keeps unchanged",
    )
    parser.add_argument(
        "--cause-anchor-file",
        default="",
        help="Optional text evidence to auto-upload when cause_disambiguation repeats",
    )
    parser.add_argument(
        "--cause-anchor-repeat-threshold",
        type=int,
        default=3,
        help="Repeat threshold to trigger auto-upload of cause anchor file",
    )
    parser.add_argument("--min-citations", type=int, default=2, help="Minimum legal citation count")

    parser.add_argument(
        "--strict-dialogue",
        action="store_true",
        default=True,
        help="Enable strict dialogue quality gate",
    )
    parser.add_argument(
        "--no-strict-dialogue",
        dest="strict_dialogue",
        action="store_false",
        help="Disable strict dialogue quality gate",
    )

    parser.add_argument(
        "--strict-quality",
        action="store_true",
        default=True,
        help="Enable strict document quality gate",
    )
    parser.add_argument(
        "--no-strict-quality",
        dest="strict_quality",
        action="store_false",
        help="Disable strict document quality gate",
    )

    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("[abort] interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
