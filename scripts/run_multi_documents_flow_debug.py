"""Debug runner: civil_first_instance private lending flow with multiple document deliverables.

This mirrors the E2E multi-doc test but prints progress so we can see where the workflow stalls.
It is a dev helper (not part of CI).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

# Allow `from client.*` when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient


def _case_facts() -> str:
    return (
        "原告：张三E2E_MULTI_DEBUG。\n"
        "被告：李四E2E_MULTI_DEBUG。\n"
        "案由：民间借贷纠纷。\n"
        "事实：2023-01-01，被告向原告借款人民币100000元，约定2023-12-31前归还；原告已通过银行转账交付。\n"
        "到期后被告未还，原告多次催收无果。\n"
        "证据：借条、转账记录、聊天记录。\n"
        "诉求：返还本金100000元，并按年利率6%支付逾期利息，承担诉讼费。"
    )


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
    # Support nested object overrides, e.g. overrides["profile.plaintiff"] can satisfy "profile.plaintiff.name".
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


def _auto_answer_card(card: dict, overrides: dict[str, Any], uploaded_file_ids: list[str]) -> dict[str, Any]:
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    answers: list[dict[str, Any]] = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue

        override_value = _resolve_override_value(fk, overrides)
        if override_value is not None:
            answers.append({"field_key": fk, "value": override_value})
            continue

        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))

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
                first = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
                value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
            if fk == "attachment_file_ids":
                value = default if has_default else uploaded_file_ids
            else:
                value = default if has_default else ([] if not required else uploaded_file_ids[:1])
        else:
            value = default if has_default else ("已确认" if required else None)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = True if it in {"boolean", "bool"} else "已确认"

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    evidence_dir = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "lawyer_workbench"
        / "civil_prosecution"
        / "evidence"
    )
    paths = [
        evidence_dir / "iou.txt",
        evidence_dir / "sample_transfer_record.txt",
        evidence_dir / "sample_chat_record.txt",
    ]

    selected_docs = [
        "civil_complaint",
        "litigation_strategy_report",
        "evidence_list_doc",
        "preservation_application",
    ]

    overrides = {
        "profile.facts": _case_facts(),
        "profile.claims": "返还本金100000元，并按年利率6%支付逾期利息，承担诉讼费。",
        "profile.decisions.selected_documents": selected_docs,
    }

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        sess = await c.create_session(service_type_id="civil_first_instance")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        # Mirror frontend behavior: bind uploads to the session first, then kickoff with facts + attachments.
        uploaded_file_ids: list[str] = []
        for p in paths:
            up = await c.upload_session_attachment(sid, str(p))
            # consultations-service returns an attachment item:
            # - data.id      -> attachment_id (NOT the files-service file_id)
            # - data.file_id -> files-service file_id (what chat/analysis expects)
            up_data = up.get("data") or {}
            fid = str((up_data.get("file_id") or up_data.get("fileId") or "")).strip()
            if not fid:
                raise RuntimeError(f"upload_session_attachment did not return file_id: {up}")
            print("uploaded", p.name, fid, flush=True)
            uploaded_file_ids.append(fid)

        matter_id = None
        t_start = time.time()
        kickoff_sent = False

        for i in range(260):
            sess2 = await c.get_session(sid)
            matter_id = (sess2.get("data") or {}).get("matter_id") or matter_id

            if matter_id:
                all_ready = True
                for key in selected_docs:
                    dels = await c.list_deliverables(str(matter_id), output_key=key)
                    data = dels.get("data") or {}
                    deliverables = data.get("deliverables") if isinstance(data, dict) else None
                    deliverables = deliverables if isinstance(deliverables, list) else []
                    d0 = deliverables[0] if deliverables and isinstance(deliverables[0], dict) else {}
                    if not deliverables or not str(d0.get("file_id") or "").strip():
                        all_ready = False
                        break
                if all_ready:
                    print("ALL deliverables ready at iter", i, "elapsed", round(time.time() - t_start, 2), "s", flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                skill_id = str(card.get("skill_id") or "").strip()
                print("iter", i, "card", card.get("task_key"), card.get("review_type"), skill_id, flush=True)
                if skill_id == "system:kickoff":
                    t0 = time.time()
                    await c.chat(sid, str(overrides["profile.facts"]), attachments=list(uploaded_file_ids), max_loops=6)
                    kickoff_sent = True
                    print("  kickoff chat", round(time.time() - t0, 2), "s", flush=True)
                else:
                    t0 = time.time()
                    await c.resume(sid, _auto_answer_card(card, overrides, uploaded_file_ids), pending_card=card)
                    print("  resume", round(time.time() - t0, 2), "s", flush=True)
                continue

            # Some flows may not immediately surface a kickoff card; send facts once to bootstrap.
            if not kickoff_sent:
                print("iter", i, "no card -> kickoff", "matter_id", matter_id, flush=True)
                t0 = time.time()
                await c.chat(sid, str(overrides["profile.facts"]), attachments=list(uploaded_file_ids), max_loops=6)
                kickoff_sent = True
                print("  kickoff chat", round(time.time() - t0, 2), "s", flush=True)
            else:
                # Avoid spamming "继续" (token-costly and can perturb the workflow); just poll.
                print("iter", i, "no card -> wait", "matter_id", matter_id, flush=True)
                await asyncio.sleep(3.0)

        print("final matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
