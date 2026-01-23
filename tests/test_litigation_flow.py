"""诉讼流程端到端测试（咨询/会话 -> 事项工作流 -> 文书生成）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest


def _dbg(*args) -> None:
    if os.getenv("E2E_DEBUG") == "1":
        print(*args, flush=True)


def _unwrap(resp: dict) -> object:
    """ApiResponse/PageResponse 统一解包：返回 resp['data']。"""
    if isinstance(resp, dict) and "code" in resp:
        return resp.get("data")
    return resp


def _pick_recommended_or_first(options: list) -> object | None:
    if not isinstance(options, list) or not options:
        return None
    for opt in options:
        if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
            return opt.get("value")
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") is not None:
            return opt.get("value")
    return None


def _auto_answer_card(card: dict, uploaded_file_ids: list[str] | None = None) -> dict:
    """基于 card.questions 自动构造 resume.user_response。"""
    skill_id = str(card.get("skill_id") or "").strip()
    uploaded_file_ids = [str(x).strip() for x in (uploaded_file_ids or []) if str(x).strip()]
    questions = card.get("questions")
    questions = questions if isinstance(questions, list) else []
    answers: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue

        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))
        qtext = str(q.get("question") or "").strip()

        value = None
        default = q.get("default")
        has_default = default is not None and not (
            (isinstance(default, str) and not default.strip())
            or (isinstance(default, list) and not default)
            or (isinstance(default, dict) and not default)
        )
        if it in {"boolean", "bool"}:
            value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            value = default if has_default else _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            if has_default:
                value = default
            else:
                options = q.get("options") if isinstance(q.get("options"), list) else []
                rec = []
                for opt in options:
                    if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
                        rec.append(opt.get("value"))
                if rec:
                    value = rec
                else:
                    first = _pick_recommended_or_first(options)
                    value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
            # 文件列表：对 attachment_file_ids，默认认为“用户已上传的证据可以复用”（更贴近前端行为）；
            # 其他 file_id(s) 字段仅在 kickoff/必填时自动回填，避免把“旧证据”误当作“新增缺口材料”导致反复补问。
            if has_default:
                value = default
            elif uploaded_file_ids and (fk == "attachment_file_ids" or required or skill_id == "system:kickoff"):
                value = uploaded_file_ids
            else:
                value = None
        else:
            # 针对关键画像字段给“更像真实案件”的默认值，避免流程卡在 intake/evidence。
            if fk == "profile.summary":
                value = "借款日期：2025-01-01；约定还款日期：2025-06-01。"
            elif fk == "profile.facts":
                value = "借条、转账记录、聊天记录已提供/可继续补充。"
            elif fk == "profile.claims":
                value = ["返还借款本金10万元", "支付逾期利息（按年利率4%暂计）"]
            elif fk == "profile.plaintiff":
                value = {"name": "张三"}
            elif fk == "profile.defendant":
                value = {"name": "李四"}
            elif fk == "profile.applicant":
                value = {"name": "张三"}
            elif fk == "profile.respondent":
                value = {"name": "李四"}
            elif fk == "profile.appellant":
                value = {"name": "张三"}
            elif fk == "profile.appellee":
                value = {"name": "李四"}
            elif fk == "profile.target_entity":
                value = "某某公司"
            elif fk == "profile.dd_scope":
                value = ["工商与主体", "重大合同", "诉讼仲裁与行政"]
            elif fk == "profile.opinion_topic":
                value = "民间借贷纠纷：起诉要点与证据要求"
            elif fk.startswith("profile.facts."):
                # 诉讼 intake 常见补问：用更贴近语义的默认答复，减少重复卡片。
                if any(k in qtext for k in ("误工", "工资", "月薪", "收入", "流水")):
                    value = "暂无误工证明/工资流水，后续可补充；目前月薪约8000元，误工约1个月。"
                elif any(k in qtext for k in ("索赔", "协商", "沟通", "回应")):
                    value = "已向对方提出口头索赔/反映，暂未收到明确回复。"
                elif any(k in qtext for k in ("费用", "发票", "单据", "凭证")):
                    value = "费用单据目前部分缺失，后续可补充。"
                else:
                    value = "待补充"
            # 文本类：给一个最小可用答复，避免 required 校验失败
            value = default if has_default else (value if value is not None else "已确认")

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            # 强兜底：必填但未生成可用值时，给一个最小占位，保证类型尽量正确。
            if it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
                value = uploaded_file_ids if uploaded_file_ids else []
            else:
                value = True if it in {"boolean", "bool"} else "已确认"

        # 模拟前端：未填写/空文件数组不提交该字段
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


@pytest.mark.e2e
@pytest.mark.slow
async def test_litigation_civil_prosecution_to_document_generation(lawyer_client):
    """
    目标：跑通「民事起诉（原告）」从收案 -> 案由确认 -> 策略确认 -> 文书选择 -> 文书生成/审核。
    """
    # 1) 上传一组最小证据包：借条 + 转账记录 + 催款聊天记录
    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures"
    paths = [
        fixture_dir / "sample_iou.pdf",
        fixture_dir / "sample_transfer_record.txt",
        fixture_dir / "sample_chat_record.txt",
    ]
    uploaded_file_ids: list[str] = []
    for p in paths:
        upload_resp = await lawyer_client.upload_file(str(p), purpose="consultation")
        uploaded = _unwrap(upload_resp)
        assert isinstance(uploaded, dict)
        fid = str(uploaded.get("id") or "").strip()
        assert fid, f"upload failed: {upload_resp}"
        uploaded_file_ids.append(fid)

    # 2) 创建会话（绑定诉讼 playbook）
    # NOTE: service_type_id is the resolved service type id (not playbook id).
    sess_resp = await lawyer_client.create_session(service_type_id="civil_prosecution")
    sess = _unwrap(sess_resp)
    assert isinstance(sess, dict)
    session_id = str(sess.get("id") or "").strip()
    assert session_id

    # 3) 发起第一轮对话
    await lawyer_client.chat(
        session_id,
        "我叫张三，要起诉李四民间借贷纠纷。李四向我借款10万元，到期不还。"
        "我有借条、转账记录、聊天记录。诉求：返还借款10万元并支付利息。",
        # 不在首轮把附件喂给 LLM（避免 file parse + 大上下文导致首轮过慢）；
        # 后续若出现“必填上传”卡片，再把已上传的 file_id 回填。
        attachments=[],
        max_loops=12,
    )

    # 4) 循环处理：卡片 -> 自动提交 -> 继续推进
    matter_id = None
    for _ in range(20):
        # 刷新会话拿到 matter_id
        sess_resp = await lawyer_client.get_session(session_id)
        sess = _unwrap(sess_resp)
        if isinstance(sess, dict) and sess.get("matter_id") is not None:
            matter_id = str(sess.get("matter_id")).strip()

        # 若已生成起诉状交付物，则认为跑通
        if matter_id:
            deliverables_resp = await lawyer_client.list_deliverables(matter_id, output_key="civil_complaint")
            deliverables = _unwrap(deliverables_resp)
            if isinstance(deliverables, dict):
                # matters-service 返回字段为 deliverables（不是 items）
                items = deliverables.get("deliverables") if isinstance(deliverables.get("deliverables"), list) else []
                if items:
                    break

        # 查看是否有 pending card
        pending_resp = await lawyer_client.get_pending_card(session_id)
        card = _unwrap(pending_resp)
        if isinstance(card, dict) and card:
            user_response = _auto_answer_card(card, uploaded_file_ids)
            await lawyer_client.resume(session_id, user_response)
            continue

        # 无卡片：给一个“继续”触发下一步
        await lawyer_client.chat(session_id, "继续", attachments=[], max_loops=12)

    assert matter_id, "session did not bind to matter"

    deliverables_resp = await lawyer_client.list_deliverables(matter_id, output_key="civil_complaint")
    deliverables = _unwrap(deliverables_resp)
    assert isinstance(deliverables, dict), deliverables_resp
    items = deliverables.get("deliverables") if isinstance(deliverables.get("deliverables"), list) else []
    assert items, f"expected civil_complaint deliverable, got: {deliverables_resp}"


def _bus_injury_facts_text() -> str:
    # Keep it rich enough so intake can extract plaintiff/defendant/facts/claims in one pass,
    # and ideally enter claim_path without asking too many extra questions.
    return (
        "原告：张三（男，1990-01-01生，身份证号110101199001011234，电话13800000000，住址北京市朝阳区XX路XX号）。\n"
        "被告：某市公交集团有限公司（统一社会信用代码91110101MA0000000X，住所北京市朝阳区XX大道XX号）。\n"
        "事故经过：2025-01-10 08:35，我乘坐123路公交车（车牌京A-12345）在东门站上车。车辆未完全停稳即起步，"
        "随后突然急刹，我在车厢内摔倒受伤。现场有乘客证人；车内监控/行车记录仪可调取。\n"
        "伤情：左踝骨折、软组织挫伤；2025-01-10入院，2025-01-15出院。\n"
        "损失：医疗费18500元、误工费8000元（按月薪8000元暂计1个月）、护理费3000元、交通费500元、营养费1000元。\n"
        "诉求：请求判令被告赔偿上述损失合计约31000元并承担诉讼费。\n"
        "证据：公交车票、住院证明/病历摘要、费用单据；后续补充行车记录仪资料证明司机过错。"
    )


def _auto_answer_card_with_overrides(card: dict, *, overrides: dict, uploaded_file_ids: list[str] | None = None) -> dict:
    def _resolve_override_value(field_key: str) -> object | None:
        if not isinstance(overrides, dict) or not overrides:
            return None
        if field_key in overrides:
            return overrides[field_key]
        # Support dot-path extraction from dict overrides, e.g.:
        # overrides["profile.defendant"] = {"name": "..."} -> field_key "profile.defendant.name"
        for k, v in overrides.items():
            if not isinstance(k, str) or not k:
                continue
            if not isinstance(v, dict):
                continue
            prefix = f"{k}."
            if not field_key.startswith(prefix):
                continue
            rest = field_key[len(prefix) :]
            cur: object = v
            ok = True
            for part in rest.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok:
                return cur
        return None

    def _coerce_value_for_question(value: object, q: dict) -> object:
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        if it in {"text", "textarea", "string"}:
            if isinstance(value, list):
                parts = [str(x).strip() for x in value if str(x).strip()]
                return "\n".join(parts)
            if isinstance(value, dict):
                # Keep it readable in logs + compatible with server-side string fields.
                return json.dumps(value, ensure_ascii=False)
        return value

    base = _auto_answer_card(card, uploaded_file_ids)
    base_answers = base.get("answers") if isinstance(base.get("answers"), list) else []
    by_fk: dict[str, dict] = {}
    for it in base_answers:
        if not isinstance(it, dict):
            continue
        fk = str(it.get("field_key") or "").strip()
        if fk:
            by_fk[fk] = it

    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    question_fks: list[str] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        question_fks.append(fk)
        ov = _resolve_override_value(fk)
        if ov is None:
            continue
        ov = _coerce_value_for_question(ov, q)
        # 模拟前端：未填写/空数组不提交该字段
        if ov is None:
            continue
        if isinstance(ov, str) and not ov.strip():
            continue
        if isinstance(ov, list) and not ov:
            continue
        if fk in by_fk:
            by_fk[fk]["value"] = ov
        else:
            by_fk[fk] = {"field_key": fk, "value": ov}

    # Rebuild answers in question order (stable, closer to frontend submission).
    out: list[dict] = []
    for fk in question_fks:
        if fk in by_fk:
            out.append(by_fk[fk])
    # Keep any extra base answers (should be rare).
    for fk, it in by_fk.items():
        if fk not in set(question_fks):
            out.append(it)

    base["answers"] = out
    return base


def _extract_sse_nodes(events: list[dict]) -> list[str]:
    nodes: list[str] = []
    for e in events or []:
        if not isinstance(e, dict) or e.get("event") != "task_start":
            continue
        data = e.get("data")
        if isinstance(data, dict):
            node = str(data.get("node") or "").strip()
            if node:
                nodes.append(node)
    return nodes


def _extract_last_sse_card(sse: dict) -> dict | None:
    events = sse.get("events") if isinstance(sse, dict) and isinstance(sse.get("events"), list) else []
    for e in reversed(events):
        if not isinstance(e, dict) or e.get("event") != "card":
            continue
        data = e.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def _question_sig(q: dict) -> str:
    fk = str(q.get("field_key") or "").strip()
    it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
    return f"{fk}|{it}"


def _card_sig(card: dict) -> str:
    skill = str(card.get("skill_id") or "").strip()
    task = str(card.get("task_key") or "").strip()
    review = str(card.get("review_type") or "").strip()
    qs = card.get("questions") if isinstance(card.get("questions"), list) else []
    sigs = [_question_sig(q) for q in qs if isinstance(q, dict)]
    raw = json.dumps({"skill": skill, "task": task, "review": review, "questions": sigs}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@pytest.mark.e2e
@pytest.mark.slow
async def test_litigation_bus_injury_reaches_claim_path_and_evidence_updates_profile(lawyer_client):
    """
    场景：民事起诉（一审原告）- 乘坐公交车受伤。

    关注点：
    - 首个卡片提交“足够丰富的案情 + 初始证据”后，是否能快速进入「案由确认（cause-recommendation）」阶段；
    - SSE 是否能产生可用于前端“思考/处理中”的 task_start/tool_start 事件；
    - matter 落库的 profile_snapshot 是否足够丰富/准确（通过 internal workflow profile 读取）；
    - 在证据分析阶段补充“司机过错”证据（dashcam_driver_fault.mp4）后，profile/阶段是否合理更新（允许触发回退或继续）。
    """
    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures"

    # 1) 初始证据：公交车票 + 住院证明
    initial_paths = [
        fixture_dir / "bus_ticket.pdf",
        fixture_dir / "hospital_certificate.pdf",
    ]
    uploaded_file_ids: list[str] = []
    for p in initial_paths:
        upload_resp = await lawyer_client.upload_file(str(p), purpose="consultation")
        uploaded = _unwrap(upload_resp)
        assert isinstance(uploaded, dict)
        fid = str(uploaded.get("id") or "").strip()
        assert fid, f"upload failed: {upload_resp}"
        uploaded_file_ids.append(fid)
    _dbg("initial evidence file_ids:", uploaded_file_ids)

    # 2) 创建会话（民事起诉一审原告 playbook）
    sess_resp = await lawyer_client.create_session(service_type_id="civil_prosecution")
    sess = _unwrap(sess_resp)
    assert isinstance(sess, dict)
    session_id = str(sess.get("id") or "").strip()
    assert session_id
    _dbg("session_id:", session_id)

    # 3) 等待后端自动 kickoff 产出第一张卡片，然后在“第一个弹框”里填入足够案情与证据
    kickoff_card = None
    for _ in range(30):
        pending_resp = await lawyer_client.get_pending_card(session_id)
        card = _unwrap(pending_resp)
        if isinstance(card, dict) and card:
            kickoff_card = card
            break
        await asyncio.sleep(1.0)
    assert isinstance(kickoff_card, dict) and kickoff_card, "expected kickoff card but got none"
    _dbg("kickoff card:", kickoff_card.get("skill_id"), kickoff_card.get("task_key"), kickoff_card.get("review_type"))

    facts = _bus_injury_facts_text()
    kickoff_overrides = {
        "profile.facts": facts,
        "profile.summary": "公交车内摔倒受伤，拟向公交公司主张赔偿",
        "profile.plaintiff": {"name": "张三"},
        "profile.plaintiff.name": "张三",
        "profile.defendant": {"name": "某市公交集团有限公司"},
        "profile.defendant.name": "某市公交集团有限公司",
        # 部分卡片将 claims 作为自由文本收集（textarea），这里提供可读的诉请摘要。
        "profile.claims": "医疗费18500元、误工费8000元、护理费3000元、交通费500元、营养费1000元（合计约31000元）",
    }
    t0 = time.time()
    kickoff_sse = await lawyer_client.resume(
        session_id, _auto_answer_card_with_overrides(kickoff_card, overrides=kickoff_overrides, uploaded_file_ids=uploaded_file_ids)
    )
    _dbg("kickoff resume took:", round(time.time() - t0, 2), "s")
    assert isinstance(kickoff_sse, dict)
    kickoff_events = kickoff_sse.get("events") if isinstance(kickoff_sse.get("events"), list) else []
    assert any(e.get("event") == "task_start" for e in kickoff_events if isinstance(e, dict)), "expected task_start SSE events"
    kickoff_next_card = _extract_last_sse_card(kickoff_sse)

    # 4) 推进到案由确认卡片（cause-recommendation）
    matter_id: str | None = None
    cause_card: dict | None = None
    seen_cards: list[dict] = []
    clarify_question_sigs: set[str] = set()
    card_sig_counts: dict[str, int] = {}
    next_card: dict | None = kickoff_next_card if isinstance(kickoff_next_card, dict) else None

    for _ in range(30):
        t_sess = time.time()
        sess = _unwrap(await lawyer_client.get_session(session_id))
        _dbg("get_session took:", round(time.time() - t_sess, 2), "s")
        if isinstance(sess, dict) and sess.get("matter_id") is not None:
            matter_id = str(sess.get("matter_id")).strip()

        if next_card is None:
            # 后端在 resume/chat 后可能需要一些时间生成下一张卡片：短轮询避免“继续”狂刷导致状态混乱。
            for _ in range(6):
                pending_resp = await lawyer_client.get_pending_card(session_id)
                pending = _unwrap(pending_resp)
                if isinstance(pending, dict) and pending:
                    next_card = pending
                    break
                await asyncio.sleep(1.0)

            if next_card is None:
                # 仍无 pending card：发一个“继续”让系统往下走，并从 SSE 中捕获卡片（部分情况下不落到 pending_card 接口）。
                t_chat = time.time()
                chat_sse = await lawyer_client.chat(session_id, "继续", attachments=[], max_loops=12)
                _dbg("chat(继续) took:", round(time.time() - t_chat, 2), "s")
                card_from_chat = _extract_last_sse_card(chat_sse)
                if isinstance(card_from_chat, dict) and card_from_chat:
                    next_card = card_from_chat
                else:
                    await asyncio.sleep(1.0)
                continue

        pending = next_card
        next_card = None

        sig = _card_sig(pending)
        card_sig_counts[sig] = card_sig_counts.get(sig, 0) + 1

        skill = str(pending.get("skill_id") or "").strip()
        review = str(pending.get("review_type") or "").strip()
        task = str(pending.get("task_key") or "").strip()
        qs = pending.get("questions") if isinstance(pending.get("questions"), list) else []

        seen_cards.append(
            {
                "skill_id": skill,
                "review_type": review,
                "task_key": task,
                "sig": sig[:12],
                "question_sigs": [_question_sig(q) for q in qs if isinstance(q, dict)],
            }
        )
        _dbg("pending card:", {"skill_id": skill, "review_type": review, "task_key": task, "q": len(qs)})

        # 防止卡死：同一张卡（相同 skill/task + questions schema）重复出现 N 次，直接失败并打印详情便于定位。
        if card_sig_counts[sig] > 3:
            raise AssertionError(
                f"same pending card repeated too many times (>3): {seen_cards[-1]}\n"
                f"card={json.dumps(pending, ensure_ascii=False)}"
            )

        if review == "clarify":
            for q in qs:
                if isinstance(q, dict):
                    qsig = _question_sig(q)
                    if qsig:
                        clarify_question_sigs.add(qsig)

        if skill == "cause-recommendation" and review == "select":
            # 质量基线：公交乘客在运输过程中受伤，默认应优先推荐“运输合同纠纷”，
            # 而不是把“机动车交通事故责任纠纷”误排在更前。
            select_q = None
            for q in qs:
                if not isinstance(q, dict):
                    continue
                if str(q.get("field_key") or "").strip() == "profile.cause_of_action_code":
                    select_q = q
                    break
            assert isinstance(select_q, dict), f"expected select question for cause_of_action_code, got: {pending}"
            options = select_q.get("options") if isinstance(select_q.get("options"), list) else []
            assert options, f"expected cause options, got: {select_q}"
            top = options[0] if isinstance(options[0], dict) else {}
            assert str(top.get("value") or "").strip() == "transport_contract", (
                f"expected top cause to be transport_contract, got: {top}\n"
                f"options={json.dumps(options[:3], ensure_ascii=False)}\n"
                f"card={json.dumps(pending, ensure_ascii=False)}"
            )

            cause_card = pending
            break

        # 继续自动补齐（若还有 intake/clarify），同时从 SSE 里抓取下一张卡片
        t_resume = time.time()
        resume_sse = await lawyer_client.resume(
            session_id,
            _auto_answer_card_with_overrides(pending, overrides=kickoff_overrides, uploaded_file_ids=uploaded_file_ids),
        )
        _dbg("resume took:", round(time.time() - t_resume, 2), "s")
        card_from_resume = _extract_last_sse_card(resume_sse)
        if isinstance(card_from_resume, dict) and card_from_resume:
            next_card = card_from_resume

    assert matter_id, "session did not bind to matter"
    assert isinstance(cause_card, dict) and cause_card, f"did not reach cause-recommendation, seen cards: {seen_cards}"

    # 约束：案情足够丰富时，不应出现过多“缺口追问”问题（参考质量基线 cases/lit_traffic_accident_01.json: questions_max=3）
    assert len(clarify_question_sigs) <= 3, f"too many clarify questions before claim_path: {sorted(clarify_question_sigs)}"

    # 5) 确认案由（走 resume），并等待进入证据分析阶段
    cause_sse = await lawyer_client.resume(session_id, _auto_answer_card(cause_card, uploaded_file_ids))
    assert isinstance(cause_sse, dict)
    cause_events = cause_sse.get("events") if isinstance(cause_sse.get("events"), list) else []
    nodes = _extract_sse_nodes([e for e in cause_events if isinstance(e, dict)])
    partial = any(
        isinstance(e, dict)
        and e.get("event") == "error"
        and isinstance(e.get("data"), dict)
        and e["data"].get("partial") is True
        for e in cause_events
    )
    assert nodes or partial, "expected task_start nodes (or partial SSE stream) for cause confirmation"

    # 读取 matter workflow profile（internal）用于检查落库画像
    prof_resp = await lawyer_client.get(f"/internal/matter-service/internal/matters/{matter_id}/workflow/profile")
    profile = _unwrap(prof_resp)
    assert isinstance(profile, dict)
    # 基本校验：facts/party/file_ids/cause_of_action_code 应该能拿到（至少部分）
    facts_saved = str(profile.get("facts") or profile.get("profile.facts") or "").strip()
    assert facts_saved, f"expected extracted facts in workflow profile, got keys: {sorted(profile.keys())}"
    file_ids_saved = profile.get("file_ids")
    assert isinstance(file_ids_saved, list) and all(str(x).strip() for x in file_ids_saved), "expected file_ids in workflow profile"
    for fid in uploaded_file_ids:
        assert fid in file_ids_saved, f"expected initial evidence file_id in matter.file_ids: {fid}"

    # 等待推进到 evidence 阶段（或已直接推进到后续阶段）
    current_phase = None
    for _ in range(40):
        timeline_resp = await lawyer_client.get(f"/api/v1/matters/{matter_id}/phase-timeline")
        tl = _unwrap(timeline_resp)
        if isinstance(tl, dict):
            current_phase = str(tl.get("current_phase") or "").strip() or None
            if current_phase in {"evidence", "strategy", "execute", "close"}:
                break
        await asyncio.sleep(1.0)
    assert current_phase in {"evidence", "strategy", "execute", "close"}, (
        f"expected to reach evidence (or later) phase, got current_phase={current_phase}"
    )

    # 6) 证据分析阶段补充“司机过错”证据（dashcam mp4），并观察画像/阶段变化
    dashcam_path = fixture_dir / "dashcam_driver_fault.mp4"
    dashcam_upload = _unwrap(await lawyer_client.upload_file(str(dashcam_path), purpose="consultation"))
    assert isinstance(dashcam_upload, dict)
    dashcam_fid = str(dashcam_upload.get("id") or "").strip()
    assert dashcam_fid

    # 先清空当前可能存在的 pending card（避免处于 interrupt 状态时无法 chat 附件）
    for _ in range(10):
        pending_resp = await lawyer_client.get_pending_card(session_id)
        card = _unwrap(pending_resp)
        if isinstance(card, dict) and card:
            await lawyer_client.resume(
                session_id,
                _auto_answer_card_with_overrides(card, overrides=kickoff_overrides, uploaded_file_ids=uploaded_file_ids + [dashcam_fid]),
            )
            continue
        break

    # 再以“新增证据”发起一轮 chat（会把 dashcam_fid 追加到 matter.file_ids，并触发 file-classify/evidence-analysis 的增量处理）
    dashcam_sse = await lawyer_client.chat(
        session_id,
        "补充证据：行车记录仪资料显示司机未停稳即起步、突然急刹，且疑似使用手机，属于明显过错。",
        attachments=[dashcam_fid],
        max_loops=12,
    )
    assert isinstance(dashcam_sse, dict)
    dashcam_events = dashcam_sse.get("events") if isinstance(dashcam_sse.get("events"), list) else []
    _dbg(
        "dashcam sse events:",
        [e.get("event") for e in dashcam_events if isinstance(e, dict)][:12],
        "output_len:",
        len(str(dashcam_sse.get("output") or "")),
    )
    dashcam_partial = any(
        isinstance(e, dict)
        and e.get("event") == "error"
        and isinstance(e.get("data"), dict)
        and e["data"].get("partial") is True
        for e in dashcam_events
    )
    assert any(e.get("event") == "task_start" for e in dashcam_events if isinstance(e, dict)) or dashcam_partial, (
        "expected task_start SSE (or partial stream) after dashcam upload"
    )

    # 处理可能出现的卡片（包括证据缺口确认/回退触发的新案由确认等）
    for _ in range(20):
        pending_resp = await lawyer_client.get_pending_card(session_id)
        card = _unwrap(pending_resp)
        if isinstance(card, dict) and card:
            await lawyer_client.resume(
                session_id,
                _auto_answer_card_with_overrides(card, overrides=kickoff_overrides, uploaded_file_ids=uploaded_file_ids + [dashcam_fid]),
            )
            continue
        break

    # 再读一次 internal workflow profile，检查 dashcam 已落入 file_ids；并记录案由字段（允许变/不变）
    prof2_resp = await lawyer_client.get(f"/internal/matter-service/internal/matters/{matter_id}/workflow/profile")
    profile2 = _unwrap(prof2_resp)
    assert isinstance(profile2, dict)
    file_ids2 = profile2.get("file_ids")
    assert isinstance(file_ids2, list) and dashcam_fid in file_ids2, "expected dashcam evidence file_id in matter.file_ids"

    # 案由字段：允许保持不变（已确认案由），也允许在新证据下触发回退并重选；至少应保持非空/一致性
    cause_code = str(profile2.get("cause_of_action_code") or "").strip()
    assert cause_code, f"expected cause_of_action_code in workflow profile, got keys: {sorted(profile2.keys())}"
