"""Debug runner: civil_prosecution end-to-end until multiple DOCX deliverables are generated.

This is a dev helper (not part of CI).

Usage:
  python e2e-tests/scripts/run_litigation_multi_docs_debug.py

Env:
  BASE_URL            default: http://localhost:18001
  LAWYER_USERNAME     default: lawyer1
  LAWYER_PASSWORD     default: lawyer123456
  SELECTED_DOC_KEYS   default: civil_complaint,litigation_strategy_report,evidence_list_doc
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import sys

# Allow `from client.*` + `from tests.*` when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import extract_docx_text, assert_docx_has_no_template_placeholders


def _split_csv(v: str) -> list[str]:
    out: list[str] = []
    for x in (v or "").split(","):
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


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


def _auto_answer_card(card: dict, uploaded_file_id: Optional[str], *, selected_docs: list[str]) -> dict:
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
        if fk == "profile.decisions.selected_documents" and it in {"multi_select", "multiple_select"}:
            allowed = []
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            allowed_set = {str(o.get("value") or "").strip() for o in opts if isinstance(o, dict)}
            for k in selected_docs:
                if k in allowed_set and k not in allowed:
                    allowed.append(k)
            # Fall back to default selection if our desired keys are not in options.
            if not allowed:
                default = q.get("default")
                if isinstance(default, list) and default:
                    allowed = [str(x).strip() for x in default if isinstance(x, (str, int)) and str(x).strip()]
            value = allowed
        elif it in {"boolean", "bool"}:
            value = True
        elif it in {"select", "single_select", "single_choice"}:
            value = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            # Prefer UI defaults (what the lawyer sees pre-selected); otherwise pick a single recommended value.
            default = q.get("default")
            if isinstance(default, list) and default:
                value = [str(x).strip() for x in default if isinstance(x, (str, int)) and str(x).strip()]
            else:
                first = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
                value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"}:
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


async def main() -> int:
    # Prefer repo-root .env, allow e2e-tests/.env to override base_url/user creds.
    root_env = Path(__file__).resolve().parent.parent.parent / ".env"
    if root_env.exists():
        load_dotenv(root_env, override=False)
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    selected_docs = _split_csv(os.getenv("SELECTED_DOC_KEYS", "civil_complaint,litigation_strategy_report,evidence_list_doc"))

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
            attachments=[file_id] if file_id else [],
            max_loops=12,
        )
        print("first chat done", round(time.time() - t0, 2), "s", flush=True)

        matter_id = None
        deliverable_file_ids: dict[str, str] = {}

        for i in range(80):
            sess2 = await c.get_session(sid)
            matter_id = (sess2.get("data") or {}).get("matter_id")

            if matter_id:
                all_ready = True
                for out_key in selected_docs:
                    dels = await c.list_deliverables(str(matter_id), output_key=out_key)
                    data = dels.get("data") if isinstance(dels, dict) else None
                    items = (data.get("deliverables") if isinstance(data, dict) else None) or []
                    if not items:
                        all_ready = False
                        continue
                    d0 = items[0] if isinstance(items[0], dict) else {}
                    fid = str(d0.get("file_id") or "").strip()
                    if not fid:
                        all_ready = False
                        continue
                    deliverable_file_ids[out_key] = fid

                if all_ready and deliverable_file_ids:
                    print("all deliverables ready at iter", i, deliverable_file_ids, flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                print("iter", i, "card", card.get("task_key"), card.get("review_type"), card.get("skill_id"), flush=True)
                t = time.time()
                await c.resume(sid, _auto_answer_card(card, file_id, selected_docs=selected_docs))
                print("  resume", round(time.time() - t, 2), "s", flush=True)
            else:
                print("iter", i, "no card -> continue", flush=True)
                t = time.time()
                await c.chat(sid, "继续", attachments=[], max_loops=12)
                print("  chat", round(time.time() - t, 2), "s", flush=True)

        print("matter_id", matter_id, flush=True)
        if not matter_id:
            print("no matter_id; abort")
            return 2

        # Download and run basic sanity checks.
        for out_key, fid in sorted(deliverable_file_ids.items()):
            b = await c.download_file_bytes(fid)
            text = extract_docx_text(b)
            assert_docx_has_no_template_placeholders(text)
            if "张三" not in text or "李四" not in text:
                print("WARN: party names not found in", out_key)
            if out_key == "litigation_strategy_report" and "行政公益诉讼" in text:
                raise AssertionError("strategy report drifted into admin/public-interest template")
            print("ok", out_key, "file_id", fid, "text_len", len(text), flush=True)

        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
