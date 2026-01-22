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
            # Default: do not upload; the first chat already includes attachments.
            value = []
            if required and uploaded_file_id:
                value = [uploaded_file_id]
        else:
            if fk == "profile.facts":
                value = (
                    "张三起诉李四民间借贷纠纷，借款10万元到期不还。"
                    "证据：借条、转账记录、聊天记录。"
                    "诉求：返还本金10万元并支付利息。"
                )
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

        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "sample_iou.pdf"
        up = await c.upload_file(str(fixture), purpose="consultation")
        file_id = str((up.get("data") or {}).get("id") or "").strip()
        print("uploaded file_id", file_id, flush=True)

        sess = await c.create_session(service_type_id="civil_prosecution")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        t0 = time.time()
        await c.chat(
            sid,
            "我叫张三，要起诉李四民间借贷纠纷。李四向我借款10万元，到期不还。"
            "我有借条（已上传）和转账记录、聊天记录。诉求：返还借款10万元并支付利息。",
            attachments=[file_id],
            max_loops=12,
        )
        print("first chat done", round(time.time() - t0, 2), "s", flush=True)

        matter_id = None
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
                print("iter", i, "card", card.get("task_key"), card.get("review_type"), card.get("skill_id"), flush=True)
                t = time.time()
                await c.resume(sid, _auto_answer_card(card, file_id))
                print("  resume", round(time.time() - t, 2), "s", flush=True)
            else:
                print("iter", i, "no card -> continue", flush=True)
                t = time.time()
                await c.chat(sid, "继续", attachments=[], max_loops=12)
                print("  chat", round(time.time() - t, 2), "s", flush=True)

        print("matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
