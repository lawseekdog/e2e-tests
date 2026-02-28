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
from dotenv import load_dotenv

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import (
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import (
    WorkbenchFlow,
    extract_last_card_from_sse,
    is_session_busy_sse,
)
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


DEFAULT_FACTS = (
    "原告：张三E2E_TPL。\n"
    "被告：李四E2E_TPL。\n"
    "案由：民间借贷纠纷。\n"
    "事实：2023-03-01，被告向原告借款人民币80000元，约定2023-10-01前归还；"
    "原告已通过银行转账交付。\n"
    "到期后被告未还，原告多次催收无果。\n"
    "证据：借条、转账记录、聊天记录。\n"
    "诉求：返还本金80000元，并按年利率6%支付逾期利息，承担诉讼费。"
)

DEFAULT_EVIDENCE_RELATIVE = (
    "tests/lawyer_workbench/civil_prosecution/evidence/sample_iou.pdf",
    "tests/lawyer_workbench/civil_prosecution/evidence/sample_transfer_record.txt",
    "tests/lawyer_workbench/civil_prosecution/evidence/sample_chat_record.txt",
)

REASONABLE_CARD_KINDS = {"clarify", "select", "confirm"}
LOW_SIGNAL_HINTS = (
    "继续",
    "处理中",
    "正在处理",
    "请稍候",
    "稍后",
    "session busy",
    "会话正在处理中",
)
CITATION_RE = re.compile(r"《[^》]{2,40}》第[一二三四五六七八九十百千万0-9]{1,8}条")
PARTY_RE = re.compile(r"(?:^|\n)\s*(?:原告|被告|申请人|被申请人|上诉人|被上诉人)\s*[:：]\s*([^\n，,。；;]{1,32})")
AMOUNT_RE = re.compile(r"(?<!\d)(\d{4,10})(?!\d)")
CLAIM_RE = re.compile(r"(?:^|\n)\s*诉求\s*[:：]\s*([^\n]+)")
CLAIM_KEYWORDS = ("返还", "支付", "逾期利息", "诉讼费", "赔偿", "承担")
PARTY_LINE_RE = re.compile(r"^\s*(原告|被告|申请人|被申请人|上诉人|被上诉人)\s*[:：]")


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _event_counts(sse: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        name = _safe_str(row.get("event")) or "unknown"
        out[name] = int(out.get(name) or 0) + 1
    return out


def _extract_templates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = unwrap_api_response(payload)
    if isinstance(data, dict) and isinstance(data.get("templates"), list):
        return [t for t in data.get("templates") if isinstance(t, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("templates"), list):
        return [t for t in payload.get("templates") if isinstance(t, dict)]
    return []


def _extract_last_cards(sse: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
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
    for line in facts_lines:
        if PARTY_LINE_RE.search(line):
            continue
        background_lines.append(line)
        if len(background_lines) >= 6:
            break
    background_text = "\n".join(background_lines).strip()
    if not background_text:
        background_text = _safe_str(facts_text)
    if len(background_text) > 520:
        background_text = background_text[:520].rstrip() + "…"

    return {
        "profile.facts": _safe_str(facts_text),
        "profile.background": background_text,
        "profile.parties": parties_text,
        "profile.summary": summary_line or "请基于已上传材料生成案件摘要。",
        "profile.claims": claim_text or "请按已提供事实整理诉求并推进起草。",
        "profile.court_name": "北京市海淀区人民法院",
        "profile.document_type": _safe_str(template_name) or "民事起诉状",
        "profile.service_type_id": _safe_str(service_type_id) or "document_drafting",
        "attachment_file_ids": [str(x).strip() for x in uploaded_file_ids if _safe_str(x)],
    }


async def _resolve_template_name(client: ApiClient, template_id: str, preferred_name: str) -> str:
    if _safe_str(preferred_name):
        return _safe_str(preferred_name)

    try:
        payload = await client.get("/templates-service/atomic/templates")
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
        detail = await client.get(f"/templates-service/templates/{template_id}")
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
    rows = data.get("deliverables") if isinstance(data, dict) and isinstance(data.get("deliverables"), list) else []
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
        try:
            return await client.create_matter(service_type_id=service_type_id, title=title, file_ids=file_ids)
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

    if not cards:
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

    if _safe_str(deliverable_status).lower() != "archived":
        failures.append(f"交付物状态未归档: status={deliverable_status}")

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


async def run(args: argparse.Namespace) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(E2E_ROOT / ".env", override=False)

    base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    template_id = _safe_str(args.template_id)
    if not template_id:
        raise ValueError("template_id is required")

    facts_text = _load_facts_text(args)
    doc_targets = _build_doc_targets(facts_text)
    output_key = _safe_str(args.output_key) or f"template:{template_id}"

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
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
    print(f"[config] service_type_id={_safe_str(args.service_type_id) or 'document_drafting'}")
    print(f"[config] template_id={template_id}")
    print(f"[config] output_key={output_key}")
    print(f"[config] output_dir={out_dir}")

    rounds: list[dict[str, Any]] = []
    cards_seen: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    dialogue_quality: dict[str, Any] | None = None
    document_quality: dict[str, Any] | None = None

    summary: dict[str, Any] = {
        "base_url": base_url,
        "username": username,
        "service_type_id": _safe_str(args.service_type_id) or "document_drafting",
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

        visible_ok = True
        visible_error = ""
        if enforce_visibility and (not busy):
            try:
                assert_visible_response(sse)
            except Exception as e:  # noqa: BLE001
                visible_ok = False
                visible_error = str(e)

        prev_streak = int(rounds[-1].get("low_signal_streak") or 0) if rounds else 0
        low_signal_streak = 0
        if (not busy) and (not cards_in_sse) and _is_low_signal_output(output_text):
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

        events = sse.get("events") if isinstance(sse.get("events"), list) else []
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
            last_sse = await flow.resume_card(card)
            payload_with_attempt = dict(payload)
            payload_with_attempt["attempt"] = attempt
            await _record_round(
                action=action,
                payload=payload_with_attempt,
                sse=last_sse if isinstance(last_sse, dict) else {},
                enforce_visibility=True,
            )
            if not is_session_busy_sse(last_sse if isinstance(last_sse, dict) else {}):
                return last_sse
            await asyncio.sleep(min(2.5, 0.4 * attempt + 0.4))
        return last_sse

    async with ApiClient(base_url) as client:
        try:
            await client.login(username, password)
            print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

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
                    service_type_id=_safe_str(args.service_type_id) or "document_drafting",
                    title=f"E2E 智能模板起草（service_type={_safe_str(args.service_type_id) or 'document_drafting'}, template_id={template_id})",
                    file_ids=uploaded_file_ids,
                    max_attempts=max(3, int(os.getenv("E2E_CREATE_MATTER_ATTEMPTS", "6") or 6)),
                )
            except Exception as e:
                # Some remote envs reject create_matter(file_ids=...) even when files are valid.
                # Fallback to creating the matter first, then rely on chat attachments in the WS flow.
                print(f"[warn] create_matter with file_ids failed, fallback without file_ids: {e}")
                matter = await _create_matter_with_retry(
                    client,
                    service_type_id=_safe_str(args.service_type_id) or "document_drafting",
                    title=f"E2E 智能模板起草（service_type={_safe_str(args.service_type_id) or 'document_drafting'}, template_id={template_id})",
                    file_ids=[],
                    max_attempts=max(3, int(os.getenv("E2E_CREATE_MATTER_ATTEMPTS", "6") or 6)),
                )
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
                },
            )
            await _record_round(
                action="workflow_action.template_draft_start",
                payload={"template_id": template_id, "output_key": output_key},
                sse=start_sse if isinstance(start_sse, dict) else {},
                enforce_visibility=False,
            )

            flow_overrides = _build_flow_overrides(
                facts_text,
                uploaded_file_ids,
                service_type_id=_safe_str(args.service_type_id) or "document_drafting",
                template_name=template_name,
            )
            flow = WorkbenchFlow(
                client=client,
                session_id=session_id,
                uploaded_file_ids=uploaded_file_ids,
                overrides=flow_overrides,
                matter_id=matter_id,
            )

            kickoff_sse = await flow.nudge(facts_text, attachments=uploaded_file_ids, max_loops=max(1, int(args.max_loops)))
            await _record_round(
                action="chat.kickoff",
                payload={"attachments": len(uploaded_file_ids)},
                sse=kickoff_sse if isinstance(kickoff_sse, dict) else {},
                enforce_visibility=True,
            )

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
            use_pending_card_api = str(os.getenv("E2E_USE_PENDING_CARD_API", "1") or "1").strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            for _ in range(max_steps):
                await flow.refresh()
                deliverable_head: dict[str, Any] | None = None
                if flow.matter_id:
                    deliverable_rows = await _list_deliverables(client, flow.matter_id, output_key)
                    if deliverable_rows:
                        deliverable_head = deliverable_rows[0]
                        candidate = _pick_deliverable_with_file(deliverable_rows)
                        if candidate:
                            deliverable_row = candidate
                            break
                    head_sig = _deliverable_signature(deliverable_head)
                    if head_sig and head_sig != last_deliverable_sig:
                        last_deliverable_sig = head_sig
                        stall_rounds = 0
                if deliverable_row:
                    break

                pending = await flow.get_pending_card() if use_pending_card_api else None
                if pending:
                    stall_rounds = 0
                    skill_id = _safe_str(pending.get("skill_id"))
                    task_key = _safe_str(pending.get("task_key"))
                    prompt_preview = _safe_str(pending.get("prompt"))[:220]
                    card_sig = f"{skill_id}|{task_key}"
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

                    # Some remote envs can loop on skill-error-analysis (docx_quality_gate_failed).
                    # Only nudge when card policy allows free chat; otherwise nudges only produce card+error loops.
                    chat_policy = pending.get("chat_policy") if isinstance(pending.get("chat_policy"), dict) else {}
                    allows_chat = bool(chat_policy.get("allows_chat"))
                    if skill_id == "skill-error-analysis" and allows_chat and last_card_repeats >= 2:
                        remediation_text = (
                            "请按失败提示修复后重试："
                            "1) 删除多余空行，段落空行率控制在20%以内；"
                            "2) 保留完整标题/当事人/请求事项/事实理由/落款结构；"
                            "3) 输出可直接交付的最终版本。"
                        )
                        remediation_sse = await flow.nudge(
                            remediation_text,
                            attachments=[],
                            max_loops=max(1, int(args.max_loops)),
                        )
                        await _record_round(
                            action="chat.skill_error_remediation",
                            payload={"repeat": last_card_repeats},
                            sse=remediation_sse if isinstance(remediation_sse, dict) else {},
                            enforce_visibility=False,
                        )
                else:
                    last_card_sig = ""
                    last_card_repeats = 0
                    if deliverable_head:
                        current_sig = _deliverable_signature(deliverable_head)
                        if (not current_sig) or current_sig == last_deliverable_sig:
                            stall_rounds += 1
                        else:
                            last_deliverable_sig = current_sig
                            stall_rounds = 0
                    else:
                        stall_rounds += 1

                    if stall_rounds >= max_stall_rounds:
                        raise AssertionError(
                            "workflow stalled with no pending card and no deliverable progress: "
                            f"stall_rounds={stall_rounds}, deliverable_status={_safe_str((deliverable_head or {}).get('status'))}"
                        )

                    if suppress_nudge_rounds > 0:
                        suppress_nudge_rounds -= 1
                        await asyncio.sleep(min(2.5, 0.3 * max(1, busy_retries) + 0.5))
                        continue

                    sse = await flow.nudge(_safe_str(args.nudge_text) or "继续", attachments=[], max_loops=max(1, int(args.max_loops)))
                    await _record_round(
                        action="chat.nudge",
                        payload={"text": _safe_str(args.nudge_text) or "继续"},
                        sse=sse if isinstance(sse, dict) else {},
                        enforce_visibility=False,
                    )

                    sse_card = extract_last_card_from_sse(sse if isinstance(sse, dict) else {})
                    if isinstance(sse_card, dict) and sse_card:
                        cards_seen.append(
                            {
                                "source": "sse",
                                "round": len(rounds) + 1,
                                "skill_id": _safe_str(sse_card.get("skill_id")),
                                "task_key": _safe_str(sse_card.get("task_key")),
                                "review_type": _safe_str(sse_card.get("review_type")),
                                "card": sse_card,
                            }
                        )
                        sse = await _resume_card_with_busy_retry(
                            flow=flow,
                            card=sse_card,
                            action="resume.sse_card",
                            payload={
                                "skill_id": _safe_str(sse_card.get("skill_id")),
                                "task_key": _safe_str(sse_card.get("task_key")),
                            },
                        )

                if is_session_busy_sse(sse if isinstance(sse, dict) else {}):
                    busy_retries += 1
                    suppress_nudge_rounds = min(24, max(suppress_nudge_rounds, 2 + busy_retries // 2))
                    await asyncio.sleep(min(2.5, 0.2 * busy_retries + 0.5))
                else:
                    busy_retries = 0
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
                confirm_sse = await client.chat(session_id, "确认", attachments=[], max_loops=max(1, int(args.max_loops)))
                await _record_round(
                    action="chat.confirm",
                    payload={"text": "确认"},
                    sse=confirm_sse if isinstance(confirm_sse, dict) else {},
                    enforce_visibility=True,
                )

            async def _archived() -> bool:
                rows = await _list_deliverables(client, flow.matter_id or matter_id, output_key)
                if not rows:
                    return False
                return _safe_str(rows[0].get("status")).lower() == "archived"

            await eventually(
                _archived,
                timeout_s=120,
                interval_s=3,
                description="deliverable archived",
            )

            rows = await _list_deliverables(client, flow.matter_id or matter_id, output_key)
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
                }
            )

            (out_dir / "deliverables.json").write_text(
                json.dumps({"deliverables": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "cards.json").write_text(
                json.dumps(cards_seen, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _write_events_ndjson(out_dir / "events.ndjson", event_rows)
            (out_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print("[done] template draft workflow completed")
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
                }
            )

            matter_id = _safe_str(summary.get("matter_id"))
            session_id = _safe_str(summary.get("session_id"))

            failure_diag: dict[str, Any] = {
                "error": str(e),
                "summary": summary,
                "rounds_tail": rounds[-10:],
                "cards_tail": cards_seen[-10:],
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
            (out_dir / "cards.json").write_text(json.dumps(cards_seen, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / "dialogue_quality.json").write_text(
                json.dumps(dialogue_quality, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "document_quality.json").write_text(
                json.dumps(document_quality, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "failure_diagnostics.json").write_text(
                json.dumps(failure_diag, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[failed] {e}")
            print(f"[artifacts] {out_dir}")
            return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run smart-template drafting workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--service-type-id", default="document_drafting", help="Matter service_type_id")
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
    parser.add_argument("--nudge-text", default="继续", help="Nudge text when no pending card")
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
