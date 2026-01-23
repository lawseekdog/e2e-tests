"""记忆提取（memory-extraction）E2E 回归用例。

目标：
- 覆盖“从用户消息抽事实 → 写入 memory-service（PG） → recall 可召回”的最短链路。
- 校验降噪（无实质内容跳过）与 PII 拦截不会回归。

注意：这些用例会触发 LLM（除 skip 用例），运行时间取决于模型与网络。
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest


AI_PLATFORM_URL = os.getenv("AI_PLATFORM_URL", "http://localhost:18084").rstrip("/")
GATEWAY_URL = os.getenv("BASE_URL", "http://localhost:18001").rstrip("/")


async def _run_memory_extract(*, user_id: int, matter_id: str, user_message: str) -> dict:
    async with httpx.AsyncClient(timeout=300.0) as c:
        resp = await c.post(
            f"{AI_PLATFORM_URL}/internal/ai/memory/extract",
            json={
                "user_id": int(user_id),
                "matter_id": str(matter_id),
                "user_message": str(user_message),
            },
        )
        resp.raise_for_status()
        body = resp.json()

    assert body.get("code") == 0, body
    data = body.get("data") or {}
    mem = data.get("memory") or {}
    assert isinstance(mem, dict), body
    return mem


async def _recall_from_memory_service(*, user_id: int, case_id: str | None, query: str, include_global: bool) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{GATEWAY_URL}/internal/memory-service/internal/memory/recall",
            json={
                "user_id": int(user_id),
                "query": str(query),
                "case_id": case_id,
                "include_global": bool(include_global),
                "limit": 10,
                # Avoid rerank in tests: keep results stable and fast.
                "use_hybrid": True,
                "use_rerank": False,
            },
        )
        resp.raise_for_status()
        return resp.json()


def _entity_keys(mem: dict) -> set[str]:
    facts = mem.get("facts") or []
    if not isinstance(facts, list):
        return set()
    out: set[str] = set()
    for f in facts:
        if isinstance(f, dict) and f.get("entity_key"):
            out.add(str(f["entity_key"]))
    return out


@pytest.mark.e2e
async def test_memory_extraction_loan_case_extracts_and_recallable(client):
    """借贷案：应抽取当事人/金额/日期 + 证据（借条/转账），且 recall 可命中。"""
    user_id = int(client.user_id)
    matter_id = f"e2e-mem-loan-{uuid.uuid4()}"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message="我叫张三，对方李四，2023年1月借给他10万元，有借条和转账记录。",
    )

    assert mem.get("skipped") is False
    keys = _entity_keys(mem)
    # 证据：由 postprocess 确定性补齐，必须稳定命中
    assert "evidence:借条" in keys
    assert "evidence:转账记录" in keys
    # 关键事实：依赖 LLM 抽取，但这是最核心的回归信号
    assert "party:plaintiff:张三" in keys
    assert "party:defendant:李四" in keys
    facts = [it for it in (mem.get("facts") or []) if isinstance(it, dict)]
    assert any(
        str(it.get("entity_key") or "").startswith("amount:")
        and "10" in str(it.get("content") or "")
        and "万" in str(it.get("content") or "")
        for it in facts
    )
    assert any(
        str(it.get("entity_key") or "").startswith("date:")
        and "2023" in str(it.get("content") or "")
        and "1月" in str(it.get("content") or "")
        for it in facts
    )

    recalled = await _recall_from_memory_service(user_id=user_id, case_id=matter_id, query="借条", include_global=False)
    assert recalled.get("total_recalled", 0) >= 1
    assert any((it.get("entity_key") == "evidence:借条") for it in (recalled.get("facts") or []) if isinstance(it, dict))


@pytest.mark.e2e
async def test_memory_extraction_labor_case_extracts_evidence_and_wage_signals(client):
    """劳动欠薪：证据（劳动合同/考勤）稳定命中；金额/期限至少命中一个。"""
    user_id = int(client.user_id)
    matter_id = f"e2e-mem-labor-{uuid.uuid4()}"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message="我在某某公司上班，2022年3月入职，月薪15000元，目前拖欠3个月工资，有劳动合同和考勤记录。",
    )

    keys = _entity_keys(mem)
    assert "evidence:劳动合同" in keys
    assert "evidence:考勤记录" in keys

    facts = [it for it in (mem.get("facts") or []) if isinstance(it, dict)]
    has_salary = any(
        ("15000" in str(it.get("content") or "") and (str(it.get("entity_key") or "").startswith("amount:"))) for it in facts
    )
    has_duration = any(
        ("3" in str(it.get("content") or "") and (str(it.get("entity_key") or "").startswith("duration:"))) for it in facts
    )
    assert has_salary or has_duration

    recalled = await _recall_from_memory_service(user_id=user_id, case_id=matter_id, query="考勤记录", include_global=False)
    assert any((it.get("entity_key") == "evidence:考勤记录") for it in (recalled.get("facts") or []) if isinstance(it, dict))


@pytest.mark.e2e
async def test_memory_extraction_skip_trivial_message(client):
    """无实质内容：应直接跳过（不写入）。"""
    user_id = int(client.user_id)
    matter_id = f"e2e-mem-skip-{uuid.uuid4()}"

    mem = await _run_memory_extract(user_id=user_id, matter_id=matter_id, user_message="继续")
    assert mem.get("skipped") is True
    assert int(mem.get("stored_count") or 0) == 0
    assert mem.get("facts") == []


@pytest.mark.e2e
async def test_memory_extraction_blocks_sensitive_pii_but_keeps_other_facts(client):
    """包含手机号/身份证号时：不能落库敏感信息，但其他事实仍可落库。"""
    user_id = int(client.user_id)
    matter_id = f"e2e-mem-pii-{uuid.uuid4()}"
    phone = "13812345678"
    idno = "11010519491231002X"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message=f"我叫张三，手机号{phone}，身份证号{idno}。2023年1月借给李四10万元，有借条。",
    )

    keys = _entity_keys(mem)
    assert "evidence:借条" in keys  # 证明提取没有被整体跳过

    # 敏感信息不得出现在任何 entity_key/content 中
    for it in (mem.get("facts") or []):
        if not isinstance(it, dict):
            continue
        assert phone not in str(it.get("entity_key") or "")
        assert phone not in str(it.get("content") or "")
        assert idno not in str(it.get("entity_key") or "")
        assert idno not in str(it.get("content") or "")


@pytest.mark.e2e
async def test_memory_extraction_preferences_are_global_scope(client):
    """用户偏好：必须写入 global scope（避免污染 case 事实）。"""
    user_id = int(client.user_id)
    matter_id = f"e2e-mem-pref-{uuid.uuid4()}"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message="以后请用表格输出，结论放在最前面，并引用到具体法条号。",
    )

    facts = [it for it in (mem.get("facts") or []) if isinstance(it, dict)]
    pref = [it for it in facts if (it.get("category") == "preference")]
    assert pref, mem  # 如果偏好抽不到，后续召回会明显变差
    assert all((it.get("scope") == "global") for it in pref)

