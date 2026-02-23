"""Debug runner: legal_opinion end-to-end until legal_opinion deliverable is generated.

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


RAW_USER_STATEMENT = """1.怀化市监理公司，给赵丽珍购买了工伤保险，但是就目前了解的情况，赵丽珍不属于工作时间、和工作原因而死亡，不属于工伤亡。但是监理公司愿意出于人道主义，适当给予补偿。
2.项目部，以及施工单位，均推诿，赵丽珍属于监理公司员工，应当由监理公司承担赔偿主体责任。
3.我个人意见：
（1）赵丽珍在下班以后饮酒，不属于工作时间，不属于工作原因，不符合工伤亡的“三工”要件，不能适用工伤亡赔偿。
（2）赵丽珍是2025年6月受聘与监理公司，没有与公司签订劳务合同，但是监理公司给赵丽珍购买了社保，失业保险，以及工伤保险。成立劳动关系。
 （3）监理公司要求监理人员不得与施工单位同吃同住，避免影响监理工作。赵丽珍监理期间，吃住在工地，是否存在：监理公司管理疏漏责任？（需要你进行法律分析）
  （4）赵丽珍吃住与项目部，尽管不属于项目部工作人员，但是项目部也应当有安全防范措施义务。赵丽珍居住于项目部板房，死亡前，应该有呼救和挣扎，但是项目部没有安装监控，疏于防范突发情况，导致赵丽珍得不到及时救治，是否应当承担一定的责任？
  （5）项目部的施工单位人员，陪同赵丽珍一同饮酒。是否存在饮酒赔偿义务，由施工单位进行适当赔偿？
  （6）赵丽珍自身患有疾病，在下班后依然饮酒，其自身应当承担多大的责任？"""

CASE_FACTS = RAW_USER_STATEMENT
CASE_BACKGROUND = RAW_USER_STATEMENT


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
    if fk in {"profile.client_role", "client_role"}:
        return "client"
    if fk == "profile.service_type_id":
        return "legal_opinion"
    if fk == "data.workbench.goal":
        return "legal_opinion"
    return "已确认"


def _normalize_required_fallback(*, field_key: str, input_type: str, uploaded_file_id: str | None) -> object:
    it = _safe_str(input_type).lower()
    if it in {"boolean", "bool"}:
        return True
    if it in {"file_ids", "file_id"}:
        return [uploaded_file_id] if uploaded_file_id else []
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


def _auto_answer_card(card: dict, uploaded_file_id: Optional[str]) -> dict:
    """Auto-construct resume.user_response from card.questions.

    For this debug runner we keep answers minimal:
    - Only fill what the card asks for.
    - Do not pre-fill opinion_topic/opinion_subtype to avoid skipping legal-opinion-intake.
    """
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
                preferred = ("client", "plaintiff")
            elif fk == "profile.service_type_id":
                preferred = ("legal_opinion",)
            elif fk == "data.workbench.goal":
                preferred = ("legal_opinion",)
            value = _pick_option_value(q, preferred_values=preferred)
        elif it in {"multi_select", "multiple_select"}:
            first = _pick_option_value(q)
            value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"}:
            value = []
            if required and uploaded_file_id:
                value = [uploaded_file_id]
        else:
            value = _default_text_answer(fk)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = _normalize_required_fallback(field_key=fk, input_type=it, uploaded_file_id=uploaded_file_id)

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        # Upload a sample material so the kickoff card's required attachment_file_ids can be satisfied.
        # This also exercises files-service parsing + matter-service prepare (ZIP expand/wait) path.
        sample_doc = Path(__file__).resolve().parents[2] / "关于赵丽珍非因工死亡事件责任分析与应对策略法律意见书.docx"
        uploaded_file_id: Optional[str] = None
        if sample_doc.exists():
            up = await c.upload_file(str(sample_doc), purpose="consultation")
            uploaded_file_id = str((up.get("data") or {}).get("id") or "").strip() or None
            print("uploaded_file_id", uploaded_file_id, flush=True)

        sess = await c.create_session(service_type_id="legal_opinion")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        # Session creation already triggers a server-side kickoff in consultations-service.
        # Poll pending_card briefly so we don't accidentally double-trigger "开始办理".
        t0 = time.time()
        for _ in range(30):
            pending0 = await c.get_pending_card(sid)
            if (pending0.get("data") or {}).get("task_key"):
                break
            await asyncio.sleep(1)
        print("kickoff wait done", round(time.time() - t0, 2), "s", flush=True)

        matter_id = None
        for i in range(60):
            sess2 = await c.get_session(sid)
            matter_id = (sess2.get("data") or {}).get("matter_id")
            if matter_id:
                dels = await c.list_deliverables(str(matter_id), output_key="legal_opinion")
                data = dels.get("data") or {}
                if data.get("deliverables"):
                    print("legal_opinion deliverable ready at iter", i, flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                print(
                    "iter",
                    i,
                    "card",
                    card.get("task_key"),
                    card.get("review_type"),
                    card.get("skill_id"),
                    flush=True,
                )
                t = time.time()
                resp = await c.resume(
                    sid,
                    _auto_answer_card(card, uploaded_file_id),
                    pending_card=card,
                    card_id=_safe_str(card.get("id") or card.get("card_id")) or None,
                )
                print("  resume", round(time.time() - t, 2), "s", flush=True)
                if _is_session_busy_response(resp):
                    print("  resume busy, wait 2s", flush=True)
                    await asyncio.sleep(2)
                err = next((e for e in (resp.get("events") or []) if isinstance(e, dict) and e.get("event") == "error"), None)
                if err:
                    data = err.get("data") if isinstance(err.get("data"), dict) else {"error": err.get("data")}
                    print("  resume error", data, flush=True)
                    # Partial stream teardown (proxy/connection) is non-fatal; continue by polling state.
                    if data.get("partial") is not True:
                        # Stop early: non-partial errors are usually deterministic (validation/config).
                        break
            else:
                print("iter", i, "no card -> continue", flush=True)
                t = time.time()
                resp = await c.chat(sid, "继续", attachments=[], max_loops=12)
                print("  chat", round(time.time() - t, 2), "s", flush=True)
                if _is_session_busy_response(resp):
                    print("  chat busy, wait 2s", flush=True)
                    await asyncio.sleep(2)
                err = next((e for e in (resp.get("events") or []) if isinstance(e, dict) and e.get("event") == "error"), None)
                if err:
                    data = err.get("data") if isinstance(err.get("data"), dict) else {"error": err.get("data")}
                    print("  chat error", data, flush=True)
                    if data.get("partial") is not True:
                        break

        print("matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
