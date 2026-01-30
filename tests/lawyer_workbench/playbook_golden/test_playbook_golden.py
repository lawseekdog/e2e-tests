from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import (
    WorkbenchFlow,
    wait_for_initial_card,
)
from tests.lawyer_workbench._support.phase_timeline import unwrap_phase_timeline
from tests.lawyer_workbench._support.sse import (
    assert_task_lifecycle,
    assert_visible_response,
)
from tests.lawyer_workbench._support.timeline import round_contents, unwrap_timeline
from tests.lawyer_workbench._support.utils import unwrap_api_response

from .assertions import (
    assert_citations_deduped_and_trimmed,
    assert_deliverable_structure,
    assert_json_structure_valid,
    assert_no_placeholder_leaks,
    assert_phase_timeline_valid,
    assert_trace_has_nodes,
    assert_workflow_profile_valid,
)
from .playbook_config import PlaybookConfig, all_playbook_ids, get_playbook_config


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))
_EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"


def _load_evidence_files(config: PlaybookConfig) -> list[str]:
    paths = []
    for fname in config.evidence_files:
        p = _EVIDENCE_DIR / fname
        if p.exists():
            paths.append(str(p))
    return paths


async def _upload_evidence(lawyer_client: Any, paths: list[str]) -> list[str]:
    file_ids = []
    for p in paths:
        up = await lawyer_client.upload_file(p, purpose="consultation")
        fid = str(
            ((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or ""
        ).strip()
        if fid:
            file_ids.append(fid)
    return file_ids


async def _run_playbook_flow(
    lawyer_client: Any,
    config: PlaybookConfig,
    uploaded_file_ids: list[str],
) -> WorkbenchFlow:
    sess = await lawyer_client.create_session(service_type_id=config.service_type_id)
    session_id = str(
        ((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or ""
    ).strip()
    assert session_id, f"Failed to create session for {config.service_type_id}: {sess}"

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides=config.profile_overrides,
    )

    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card

    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)
    assert_task_lifecycle(kickoff_sse)

    if config.primary_output_key:

        async def _deliverable_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            resp = await f.client.list_deliverables(f.matter_id)
            data = unwrap_api_response(resp)
            if not isinstance(data, dict):
                return False
            items = (
                data.get("deliverables")
                if isinstance(data.get("deliverables"), list)
                else []
            )
            target_keys = {config.primary_output_key} | set(
                config.alternate_output_keys
            )
            for it in items:
                if isinstance(it, dict):
                    ok = str(it.get("output_key") or "").strip()
                    if ok in target_keys:
                        return True
            return False

        await flow.run_until(
            _deliverable_ready,
            max_steps=70,
            description=f"{config.primary_output_key} deliverable",
        )
    else:

        async def _intake_completed(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            prof_resp = await f.client.get_workflow_profile(f.matter_id)
            prof = unwrap_api_response(prof_resp)
            if not isinstance(prof, dict):
                return False
            status = str(prof.get("intake_status") or "").strip()
            return status == "completed"

        await flow.run_until(
            _intake_completed,
            max_steps=40,
            description="intake completed (consultation flow)",
        )

    return flow


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.parametrize("playbook_id", all_playbook_ids())
async def test_playbook_golden_regression(lawyer_client, playbook_id: str):
    config = get_playbook_config(playbook_id)

    evidence_paths = _load_evidence_files(config)
    if not evidence_paths:
        pytest.skip(f"No evidence files found for {playbook_id}")

    uploaded_file_ids = await _upload_evidence(lawyer_client, evidence_paths)
    assert uploaded_file_ids, f"Failed to upload evidence for {playbook_id}"

    flow = await _run_playbook_flow(lawyer_client, config, uploaded_file_ids)
    assert flow.matter_id, f"Flow did not bind to matter_id for {playbook_id}"

    mid_int = int(flow.matter_id)
    assert (
        await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int])
        == 1
    )
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_tasks where matter_id = %s",
            [mid_int],
        )
        > 0
    )

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else []
    assert isinstance(traces, list) and traces, (
        f"No traces for {playbook_id}: {traces_resp}"
    )

    if config.required_trace_nodes:
        assert_trace_has_nodes(traces, required_nodes=config.required_trace_nodes)

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), f"Invalid profile for {playbook_id}: {prof_resp}"
    assert_json_structure_valid(prof)
    assert_workflow_profile_valid(prof, service_type_id=config.service_type_id)

    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_phase_timeline_valid(
        pt, playbook_id=config.playbook_id, required_phases=config.required_phases
    )

    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    contents = round_contents(tl)
    for c in contents:
        if isinstance(c, dict):
            assert_json_structure_valid(c)

    if config.primary_output_key:
        dels_resp = await lawyer_client.list_deliverables(flow.matter_id)
        dels = unwrap_api_response(dels_resp)
        items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []

        target_keys = {config.primary_output_key} | set(config.alternate_output_keys)
        picked = None
        for it in items:
            if isinstance(it, dict):
                ok = str(it.get("output_key") or "").strip()
                if ok in target_keys:
                    picked = it
                    break

        assert picked is not None, (
            f"No deliverable found for {playbook_id}. Expected: {target_keys}. Got: {[it.get('output_key') for it in items if isinstance(it, dict)]}"
        )
        assert_deliverable_structure(picked)

        file_id = str(picked.get("file_id") or "").strip()
        assert file_id, f"Deliverable missing file_id for {playbook_id}: {picked}"

        docx_bytes = await lawyer_client.download_file_bytes(file_id)
        text = extract_docx_text(docx_bytes)

        assert_docx_has_no_template_placeholders(text)
        assert_no_placeholder_leaks(text)

        if config.docx_must_include:
            assert_docx_contains(text, must_include=config.docx_must_include)

        assert_citations_deduped_and_trimmed(text)

        assert (
            await count(
                _MATTER_DB,
                "select count(1) from matter_deliverables where matter_id = %s",
                [mid_int],
            )
            >= 1
        )
