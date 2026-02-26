"""Debug runner: civil_prosecution end-to-end until civil_complaint is generated.

This is a dev helper (not part of CI). It prints progress so we can see where the workflow stalls.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import sys

# Allow `from client.*` when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient


CASE_FACTS = (
    "张三起诉李四民间借贷纠纷。李四向张三借款10万元，到期未还。"
    "现有证据包括借条、银行转账记录、微信聊天记录。"
    "诉求为返还本金10万元并支付逾期利息。"
)

CASE_BACKGROUND = (
    "借贷发生于双方熟人关系期间，借款交付后约定期限届满仍未清偿。"
    "双方多次催收沟通未果，已形成明确争议。"
    "目前已准备起诉材料并希望完成诉讼主张、证据清单与法律依据整理。"
)

REFERENCE_QUERY = (
    "民间借贷 借条 转账记录 聊天记录 逾期还款 利息支持 "
    "最高人民法院 关于审理民间借贷案件适用法律若干问题的规定"
)


def _safe_str(value: object) -> str:
    return str(value or "").strip()


def _pick_recommended_or_first(options):
    if not isinstance(options, list) or not options:
        return None
    for opt in options:
        if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
            return opt.get("value")
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") is not None:
            return opt.get("value")
    return None


def _pick_option_value(question: dict, *, preferred_values: tuple[str, ...] = ()) -> object:
    options = question.get("options") if isinstance(question.get("options"), list) else []
    if not options:
        return None

    # Prefer explicit target values first when field semantics are known.
    for want in preferred_values:
        target = _safe_str(want).lower()
        if not target:
            continue
        for opt in options:
            if not isinstance(opt, dict):
                continue
            value = _safe_str(opt.get("value") or opt.get("id")).lower()
            if value == target:
                return opt.get("value", opt.get("id"))

    picked = _pick_recommended_or_first(options)
    if picked is not None:
        return picked

    # Last fallback: id-only option rows.
    for opt in options:
        if isinstance(opt, dict) and opt.get("id") is not None:
            return opt.get("id")
    return None


def _default_text_answer(field_key: str) -> str:
    fk = _safe_str(field_key)
    if fk == "profile.facts":
        return CASE_FACTS
    if fk == "profile.background":
        return CASE_BACKGROUND
    if fk == "profile.summary":
        return "原告张三主张被告李四民间借贷到期不还，请求返还本金10万元并支付逾期利息。"
    if fk == "profile.plaintiff":
        return "张三（出借人）"
    if fk == "profile.defendant":
        return "李四（借款人）"
    if fk == "profile.claims":
        return "1. 判令被告返还借款本金10万元；2. 判令被告支付逾期利息；3. 诉讼费由被告承担。"
    if fk == "profile.legal_issue":
        return "请求确认借款关系成立、支持本金及逾期利息。"
    if fk == "data.search.query":
        return REFERENCE_QUERY
    if fk in {"profile.client_role", "client_role"}:
        return "plaintiff"
    if fk == "profile.service_type_id":
        return "civil_prosecution"
    if fk == "data.workbench.goal":
        return "case_analysis"
    return "已确认"


def _normalize_required_fallback(*, field_key: str, input_type: str, uploaded_file_ids: list[str]) -> object:
    it = _safe_str(input_type).lower()
    if it in {"boolean", "bool"}:
        return True
    if it in {"file_ids", "file_id"}:
        return list(uploaded_file_ids) if uploaded_file_ids else []
    return _default_text_answer(field_key)


def _is_session_busy_response(resp: dict) -> bool:
    events = resp.get("events") if isinstance(resp.get("events"), list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        evt = _safe_str(row.get("event")).lower()
        data = row.get("data")
        if evt == "error":
            msg = _safe_str((data or {}).get("error") if isinstance(data, dict) else data)
            if "当前会话正在处理中" in msg:
                return True
        if evt == "end":
            output = _safe_str((data or {}).get("output") if isinstance(data, dict) else data)
            if "当前会话正在处理中" in output:
                return True
    return False


def _auto_answer_card(card: dict, uploaded_file_ids: list[str]) -> dict:
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    answers = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))

        value = None
        if it in {"boolean", "bool"}:
            value = True
        elif it in {"select", "single_select", "single_choice"}:
            preferred: tuple[str, ...] = ()
            if fk in {"profile.client_role", "client_role"}:
                preferred = ("plaintiff", "appellant")
            elif fk == "profile.service_type_id":
                preferred = ("civil_prosecution", "civil_appeal_appellant")
            elif fk == "data.workbench.goal":
                preferred = ("case_analysis", "judgment_prediction", "work_plan")
            value = _pick_option_value(q, preferred_values=preferred)
        elif it in {"multi_select", "multiple_select"}:
            first = _pick_option_value(q)
            value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"}:
            # Hard-cut workflows may require explicit file_ids for downstream evidence binding.
            value = list(uploaded_file_ids) if uploaded_file_ids else []
        else:
            value = _default_text_answer(fk)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = _normalize_required_fallback(field_key=fk, input_type=it, uploaded_file_ids=uploaded_file_ids)

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        fixture_dir = Path(__file__).resolve().parent.parent / "fixtures"
        upload_names = [
            "sample_iou.pdf",
            "sample_chat_record.txt",
            "sample_transfer_record.txt",
        ]
        uploaded_file_ids: list[str] = []
        for name in upload_names:
            up = await c.upload_file(str(fixture_dir / name), purpose="consultation")
            fid = str((up.get("data") or {}).get("id") or "").strip()
            if fid:
                uploaded_file_ids.append(fid)
            print("uploaded", name, fid, flush=True)

        sess = await c.create_session(service_type_id="civil_prosecution")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        t0 = time.time()
        await c.chat(
            sid,
            "我叫张三，要起诉李四民间借贷纠纷。李四向我借款10万元，到期不还。"
            "我有借条（已上传）和转账记录、聊天记录。诉求：返还借款10万元并支付利息。",
            attachments=uploaded_file_ids,
            max_loops=12,
        )
        print("first chat done", round(time.time() - t0, 2), "s", flush=True)

        matter_id = None
        no_card_streak = 0
        for i in range(40):
            sess2 = await c.get_session(sid)
            matter_id = (sess2.get("data") or {}).get("matter_id")
            if matter_id:
                dels = await c.list_deliverables(str(matter_id), output_key="civil_complaint")
                data = dels.get("data") or {}
                if data.get("deliverables"):
                    print("civil_complaint deliverable ready at iter", i, flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                no_card_streak = 0
                print("iter", i, "card", card.get("task_key"), card.get("review_type"), card.get("skill_id"), flush=True)
                t = time.time()
                resp = await c.resume(
                    sid,
                    _auto_answer_card(card, uploaded_file_ids),
                    pending_card=card,
                    card_id=_safe_str(card.get("id") or card.get("card_id")) or None,
                )
                print("  resume", round(time.time() - t, 2), "s", flush=True)
                if _is_session_busy_response(resp):
                    print("  resume busy, wait 2s", flush=True)
                    await asyncio.sleep(2)
            else:
                no_card_streak += 1
                # No-card loops can mean either:
                # 1) backend still busy (sending "继续" too often triggers busy errors), or
                # 2) backend idle and waiting for a nudge.
                # Use sparse nudges to avoid spamming while still allowing progression.
                if no_card_streak % 3 != 0:
                    print("iter", i, f"no card (streak={no_card_streak}) -> poll wait 4s", flush=True)
                    await asyncio.sleep(4)
                    continue

                print("iter", i, f"no card (streak={no_card_streak}) -> chat continue", flush=True)
                t = time.time()
                resp = await c.chat(sid, "继续", attachments=[], max_loops=12)
                print("  chat", round(time.time() - t, 2), "s", flush=True)
                if _is_session_busy_response(resp):
                    print("  chat busy, wait 6s", flush=True)
                    await asyncio.sleep(6)

        print("matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
