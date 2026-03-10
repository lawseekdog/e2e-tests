from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    assert_legal_opinion_docx_benchmark,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, is_session_busy_sse
from tests.lawyer_workbench._support.sse import assert_has_end, assert_has_progress, assert_task_lifecycle, collect_run_skill_ids
from tests.lawyer_workbench._support.utils import unwrap_api_response

_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
_GOLD_OPINION_DOCX = _WORKSPACE_ROOT / "关于赵丽珍非因工死亡事件责任分析与应对策略法律意见书.docx"
_LEGAL_OPINION_CAUSE_CODE = str(os.getenv("E2E_LEGAL_OPINION_CAUSE_CODE", "personal_injury_tort") or "personal_injury_tort").strip()
_RETRYABLE_HTTP_STATUS = {404, 409, 429, 500, 502, 503, 504}
_FLOW_MAX_ATTEMPTS = int(os.getenv("E2E_FLOW_MAX_ATTEMPTS", "3") or 3)
_FLOW_OVERRIDES = {
    "profile.client_role": "applicant",
    "profile.summary": "赵丽珍非因工死亡事件责任分析与应对策略法律意见。",
    "profile.facts": "赵丽珍由监理公司聘用并缴纳社保及工伤保险，下班后饮酒死亡，项目部与施工单位存在推诿。",
    "profile.opinion_subtype": "dispute_response",
    "profile.opinion_topic_primary": "accident_death",
    "profile.opinion_topics_secondary": ["labor_employment", "construction_safety"],
    "data.evidence.evidence_gap_stop_ask": True,
    "data.evidence.evidence_gap_notes": "当前暂无新增材料，请基于律师陈述先形成可补证的法律意见草稿。",
    "data.search.query": "赵丽珍 非因工死亡 工伤认定 视同工伤 劳动关系 安全保障义务 共同饮酒 责任比例 人道主义补偿",
}

_KICKOFF = """
请基于以下律师描述，在当前无附件、无其他证据材料的情况下，先形成一版可补证使用的法律意见书草稿，并明确哪些结论仍需后续证据核验：
1. 怀化市监理公司给赵丽珍购买了工伤保险，但目前了解的情况看，赵丽珍不属于工作时间、也非因工作原因死亡，不属于工伤亡；监理公司愿意出于人道主义适当给予补偿。
2. 项目部以及施工单位互相推诿，认为赵丽珍属于监理公司员工，应由监理公司承担赔偿主体责任。
3. 需要重点分析：
（1）是否不符合工伤亡“三工”要件；
（2）赵丽珍虽未签书面合同，但已受聘、并由监理公司购买社保、失业保险和工伤保险，是否成立劳动关系；
（3）监理公司要求监理人员不得与施工单位同吃同住，但赵丽珍实际吃住在工地，监理公司是否存在管理疏漏责任；
（4）赵丽珍住在项目部板房，项目部/施工单位是否负有安全保障义务，未安装监控、未及时救治是否承担责任；
（5）施工单位人员陪同赵丽珍饮酒，是否存在共同饮酒赔偿义务；
（6）赵丽珍自身疾病、下班后仍饮酒，其自身应承担多大责任。
4. 输出目标：关于赵丽珍非因工死亡事件责任分析与应对策略法律意见书。
5. 要求：
- 不能把现有陈述写成已经证实的事实；
- 必须写清不确定性与补证方向；
- 必须给出监理公司应对策略、谈判建议、责任分配思路。
""".strip()


def _is_retryable_http_error(err: httpx.HTTPStatusError) -> bool:
    code = err.response.status_code if err.response is not None else None
    return code in _RETRYABLE_HTTP_STATUS


def _must_exist(path: Path) -> None:
    assert path.exists() and path.is_file(), f"required file missing: {path}"


@pytest.mark.e2e
@pytest.mark.slow
async def test_legal_opinion_benchmark_without_materials(lawyer_client):
    _must_exist(_GOLD_OPINION_DOCX)
    required_output_keys = {"phase_summary__opinion_output", "legal_opinion"}

    async def _build_flow() -> WorkbenchFlow:
        sess = await lawyer_client.create_session(
            service_type_id="legal_opinion",
            client_role="applicant",
            cause_of_action_code=_LEGAL_OPINION_CAUSE_CODE,
            title="赵丽珍非因工死亡法律意见E2E",
        )
        session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
        assert session_id, sess
        matter_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("matter_id") or "").strip()
        return WorkbenchFlow(
            client=lawyer_client,
            session_id=session_id,
            uploaded_file_ids=[],
            overrides=dict(_FLOW_OVERRIDES),
            matter_id=matter_id or None,
        )

    flow: WorkbenchFlow | None = None
    for flow_attempt in range(1, max(1, _FLOW_MAX_ATTEMPTS) + 1):
        flow = await _build_flow()
        first_sse = await flow.nudge(_KICKOFF, attachments=[], max_loops=4)

        try:
            assert_has_end(first_sse)
            if not is_session_busy_sse(first_sse):
                assert_has_progress(first_sse)
                assert_task_lifecycle(first_sse)

            async def _opinion_ready(f: WorkbenchFlow) -> bool:
                await f.refresh()
                if not f.matter_id:
                    return False
                try:
                    resp = await f.client.list_deliverables(f.matter_id)
                    data = unwrap_api_response(resp)
                except httpx.HTTPStatusError as e:
                    if _is_retryable_http_error(e):
                        return False
                    raise
                if not isinstance(data, dict):
                    return False
                rows = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
                by_key = {
                    str(it.get("output_key") or "").strip(): it
                    for it in rows
                    if isinstance(it, dict) and str(it.get("output_key") or "").strip()
                }
                if not required_output_keys.issubset(set(by_key.keys())):
                    return False
                if not str((by_key.get("legal_opinion") or {}).get("file_id") or "").strip():
                    return False
                summary = by_key.get("phase_summary__opinion_output") or {}
                content = summary.get("content") if isinstance(summary.get("content"), dict) else {}
                md = str(content.get("markdown") or content.get("md") or content.get("content") or "").strip()
                return len(md) > 30

            await flow.run_until(_opinion_ready, max_steps=30, description="zhaolizhen legal_opinion deliverables")
            break
        except AssertionError:
            if flow_attempt >= max(1, _FLOW_MAX_ATTEMPTS):
                raise
            await asyncio.sleep(min(6.0, 1.2 * flow_attempt))

    assert flow is not None and flow.matter_id

    run_skill_ids = collect_run_skill_ids(flow.seen_sse)
    assert any("legal-opinion-intake" in sid for sid in run_skill_ids), run_skill_ids
    assert any("legal-opinion-analysis" in sid for sid in run_skill_ids), run_skill_ids
    assert any("document-generation" in sid for sid in run_skill_ids), run_skill_ids

    snapshot_resp = await lawyer_client.get(f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot")
    snapshot = unwrap_api_response(snapshot_resp)
    assert isinstance(snapshot, dict), snapshot_resp
    analysis_state = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    opinion_view = analysis_state.get("legal_opinion_view") if isinstance(analysis_state, dict) else {}
    assert isinstance(opinion_view, dict), snapshot_resp
    for key in ("summary", "issues", "key_rules", "analysis_points", "risks", "missing_materials"):
        assert key in opinion_view, opinion_view
    assert str(opinion_view.get("summary") or "").strip(), opinion_view
    assert isinstance(opinion_view.get("issues"), list) and opinion_view.get("issues"), opinion_view
    assert isinstance(opinion_view.get("analysis_points"), list) and opinion_view.get("analysis_points"), opinion_view
    assert isinstance(opinion_view.get("key_rules"), list) and opinion_view.get("key_rules"), opinion_view
    assert isinstance(opinion_view.get("missing_materials"), list), opinion_view

    deliverables_resp = await lawyer_client.list_deliverables(flow.matter_id)
    deliverables_data = unwrap_api_response(deliverables_resp)
    rows = deliverables_data.get("deliverables") if isinstance(deliverables_data, dict) else []
    assert isinstance(rows, list) and rows, deliverables_resp
    by_key = {
        str(it.get("output_key") or "").strip(): it
        for it in rows
        if isinstance(it, dict) and str(it.get("output_key") or "").strip()
    }
    assert required_output_keys.issubset(set(by_key.keys())), sorted(by_key.keys())

    report = by_key.get("legal_opinion") or {}
    report_file_id = str(report.get("file_id") or "").strip()
    assert report_file_id, report

    generated_docx = await lawyer_client.download_file_bytes(report_file_id)
    generated_text = extract_docx_text(generated_docx)
    assert_docx_has_no_template_placeholders(generated_text)
    assert_docx_contains(
        generated_text,
        must_include=["赵丽珍", "法律意见", "监理公司", "工伤", "劳动关系", "项目部", "施工单位"],
    )

    gold_text = extract_docx_text(_GOLD_OPINION_DOCX.read_bytes())
    benchmark = assert_legal_opinion_docx_benchmark(generated_text, gold_text=gold_text)
    assert benchmark.passed
