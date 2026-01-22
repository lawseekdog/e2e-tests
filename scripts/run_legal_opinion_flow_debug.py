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


CASE_FACTS = (
    "事项：赵丽珍非因工死亡事件责任分析与应对策略（非诉法律意见书）。\n"
    "\n"
    "已知情况（来自委托人陈述/目前了解）：\n"
    "1）怀化市某监理公司为赵丽珍购买了社保、失业保险及工伤保险。\n"
    "2）赵丽珍系2025年6月受聘于监理公司，未签书面劳动合同，但已参保（拟据此主张劳动关系成立）。\n"
    "3）监理公司管理要求：监理人员不得与施工单位同吃同住，避免影响监理工作；但赵丽珍监理期间吃住在工地。\n"
    "4）赵丽珍在下班后与项目部/施工单位人员饮酒后死亡；目前了解其死亡不属于工作时间、工作原因导致，不符合工伤认定“三工”要件。\n"
    "5）项目部及施工单位推诿：认为赵丽珍系监理公司员工，应由监理公司承担主要赔偿责任。\n"
    "\n"
    "拟咨询/需分析的问题：\n"
    "（1）不构成工伤亡的情况下，监理公司是否仍可能基于管理疏漏/安全保障义务承担一定责任？\n"
    "（2）赵丽珍居住在项目部板房，现场未安装监控/未及时救助，项目部是否可能承担安全保障义务或补充责任？\n"
    "（3）施工单位人员陪同饮酒，是否构成共同饮酒人的安全注意义务/侵权责任，需要适当赔偿？\n"
    "（4）赵丽珍自身患病且下班后饮酒，对损害后果的自担责任比例如何评价？\n"
    "（5）监理公司愿意出于人道主义适当补偿，建议的沟通口径、协商路径与争议升级预案。\n"
    "\n"
    "材料情况：暂未提供现场监控、急救记录、死亡原因/医学结论、饮酒同席人员陈述、住宿管理制度等证据（均待补充）。"
)

CASE_BACKGROUND = (
    "补充说明（可标注为“待核实”的部分请以材料为准）：\n"
    "1）死亡时间/地点：暂以“下班后在项目部板房/工地住宿点”描述；具体日期、发现时间、送医时间待核实。\n"
    "2）死亡原因：目前仅知饮酒后死亡；具体死因（心源性猝死/酒精中毒/窒息/跌倒外伤等）待以死亡证明、尸检或医院诊断结论为准。\n"
    "3）同席饮酒人员：项目部/施工单位人员陪同饮酒，人数、身份、劝阻/照看情况待核实。\n"
    "4）现场救助：项目部板房未装监控/缺少及时救助线索；是否有报警、呼救、急救记录待核实。\n"
    "5）合同/管理：监理公司与建设单位/施工单位之间的监理合同、驻场管理制度、住宿安排、禁止同吃同住制度的告知与执行留痕均待补充。\n"
    "6）事后处置：监理公司拟人道补偿；项目部/施工单位推诿主体责任；已沟通内容、家属诉求、是否存在工伤认定申请或侵权主张待核实。"
)


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
            value = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            first = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
            value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"}:
            value = []
            if required and uploaded_file_id:
                value = [uploaded_file_id]
        else:
            if fk == "profile.facts":
                value = CASE_FACTS
            elif fk == "profile.background":
                value = CASE_BACKGROUND
            else:
                value = "已确认"

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = True if it in {"boolean", "bool"} else "已确认"

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

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
                resp = await c.resume(sid, _auto_answer_card(card, None))
                print("  resume", round(time.time() - t, 2), "s", flush=True)
                err = next((e for e in (resp.get("events") or []) if isinstance(e, dict) and e.get("event") == "error"), None)
                if err:
                    print("  resume error", err.get("data"), flush=True)
                    # Stop early: errors are usually deterministic (validation/config) and repeated retries just spam.
                    break
            else:
                print("iter", i, "no card -> continue", flush=True)
                t = time.time()
                resp = await c.chat(sid, "继续", attachments=[], max_loops=12)
                print("  chat", round(time.time() - t, 2), "s", flush=True)
                err = next((e for e in (resp.get("events") or []) if isinstance(e, dict) and e.get("event") == "error"), None)
                if err:
                    print("  chat error", err.get("data"), flush=True)
                    break

        print("matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
