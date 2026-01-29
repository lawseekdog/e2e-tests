"""记忆提取（memory-extraction）E2E 回归用例。

目标：
- 覆盖“从用户消息抽事实 → 写入 memory-service（PG） → recall 可召回”的最短链路。
- 校验降噪（无实质内容跳过）与 PII 拦截不会回归。

注意：这些用例会触发 LLM（除 skip 用例），运行时间取决于模型与网络。
"""

from __future__ import annotations

import os
import uuid
import hashlib

import httpx
import pytest


AI_PLATFORM_URL = os.getenv("AI_PLATFORM_URL", "http://localhost:18084").rstrip("/")
GATEWAY_URL = os.getenv("BASE_URL", "http://localhost:18001").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
_PII_PHONE = "13812345678"


async def _run_memory_extract(
    *,
    user_id: int,
    matter_id: str,
    user_message: str,
    tenant_id: str,
    state_patch: dict | None = None,
) -> dict:
    if not INTERNAL_API_KEY:
        raise RuntimeError("INTERNAL_API_KEY is required for E2E ai-platform-service internal calls")
    async with httpx.AsyncClient(timeout=300.0) as c:
        payload: dict = {
            "user_id": int(user_id),
            "tenant_id": str(tenant_id or "").strip(),
            "matter_id": str(matter_id),
            "user_message": str(user_message),
        }
        patch = dict(state_patch) if isinstance(state_patch, dict) else {}
        patch["tenant_id"] = str(tenant_id or "").strip()
        payload["state_patch"] = patch
        resp = await c.post(
            f"{AI_PLATFORM_URL}/api/v1/internal/ai/memory/extract",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()

    assert body.get("code") == 0, body
    data = body.get("data") or {}
    mem = data.get("memory") or {}
    assert isinstance(mem, dict), body
    return mem


async def _recall_from_memory_service(*, user_id: int, tenant_id: str, case_id: str | None, query: str, include_global: bool) -> dict:
    if not INTERNAL_API_KEY:
        raise RuntimeError("INTERNAL_API_KEY is required for E2E memory-service internal calls")
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{GATEWAY_URL}/api/v1/internal/memory-service/memory/recall",
            headers={"X-Organization-Id": str(tenant_id or "").strip(), "X-Internal-Api-Key": INTERNAL_API_KEY},
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
        body = resp.json()
        assert body.get("code") == 0, body
        data = body.get("data") or {}
        return data if isinstance(data, dict) else {}


async def _list_case_facts_from_memory_service(*, user_id: int, tenant_id: str, case_id: str, limit: int = 200) -> list[dict]:
    if not INTERNAL_API_KEY:
        raise RuntimeError("INTERNAL_API_KEY is required for E2E memory-service internal calls")
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.get(
            f"{GATEWAY_URL}/api/v1/internal/memory-service/memory/users/{int(user_id)}/facts",
            headers={"X-Organization-Id": str(tenant_id or "").strip(), "X-Internal-Api-Key": INTERNAL_API_KEY},
            params={"scope": "case", "case_id": str(case_id), "limit": int(limit)},
        )
        resp.raise_for_status()
        body = resp.json()
        assert body.get("code") == 0, body
        data = body.get("data")
        if isinstance(data, dict):
            items = data.get("data")
            return items if isinstance(items, list) else []
        return data if isinstance(data, list) else []


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
async def test_memory_extraction_case_messages_do_not_write_case_facts(client):
    """route2：memory-extraction 不再写入 case 事实（避免与 matter 真源漂移）。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = f"e2e-mem-no-case-{uuid.uuid4()}"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message="我叫张三，对方李四，2023年1月借给他10万元，有借条和转账记录。",
        tenant_id=tenant_id,
        # Avoid triggering LLM in E2E: deterministic path is enough for this regression.
        state_patch={"_force_deterministic_memory_extraction": True},
    )

    assert mem.get("skipped") is True
    assert int(mem.get("stored_count") or 0) == 0
    assert _entity_keys(mem) == set()


@pytest.mark.e2e
async def test_memory_extraction_skip_trivial_message(client):
    """无实质内容：应直接跳过（不写入）。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = f"e2e-mem-skip-{uuid.uuid4()}"

    mem = await _run_memory_extract(user_id=user_id, tenant_id=tenant_id, matter_id=matter_id, user_message="继续")
    assert mem.get("skipped") is True
    assert int(mem.get("stored_count") or 0) == 0
    assert mem.get("facts") == []


@pytest.mark.e2e
async def test_memory_extraction_blocks_sensitive_pii_in_preferences(client):
    """包含手机号/身份证号时：不得落库敏感信息（即便同一条消息包含偏好指令）。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = f"e2e-mem-pii-{uuid.uuid4()}"
    phone = "13812345678"
    idno = "11010519491231002X"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message=f"我叫张三，手机号{phone}，身份证号{idno}。以后请用表格输出，结论放在最前面。",
        tenant_id=tenant_id,
        state_patch={"_force_deterministic_memory_extraction": True},
    )

    # 敏感信息不得出现在任何 entity_key/content 中（偏好事实也不应携带 PII）
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
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = f"e2e-mem-pref-{uuid.uuid4()}"

    mem = await _run_memory_extract(
        user_id=user_id,
        matter_id=matter_id,
        user_message="以后请用表格输出，结论放在最前面，并引用到具体法条号。",
        tenant_id=tenant_id,
        state_patch={"_force_deterministic_memory_extraction": True},
    )

    facts = [it for it in (mem.get("facts") or []) if isinstance(it, dict)]
    pref = [it for it in facts if (it.get("category") == "preference")]
    assert pref, mem  # 如果偏好抽不到，后续召回会明显变差
    assert all((it.get("scope") == "global") for it in pref)


@pytest.mark.e2e
async def test_memory_service_route2_strict_blocks_public_case_writes(client):
    """route2 服务端强约束：/api/v1/internal/memory/facts 禁止写入 case 事实（仅允许 summary:skill:* 例外）。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = f"e2e-route2-block-{uuid.uuid4()}"
    ev_iou = "evidence:hint:" + hashlib.md5("借条".encode("utf-8")).hexdigest()[:12]

    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{GATEWAY_URL}/api/v1/internal/memory-service/memory/facts",
            headers={"X-Organization-Id": tenant_id, "X-Internal-Api-Key": INTERNAL_API_KEY},
            json={
                "user_id": user_id,
                "content": "证据：借条",
                "category": "evidence",
                "scope": "case",
                "case_id": matter_id,
                "entities": {"certainty": "confirmed"},
                "entity_key": ev_iou,
                "source": {"producer": "e2e"},
            },
        )

    assert resp.status_code == 403, resp.text


@pytest.mark.e2e
async def test_memory_service_blocks_sensitive_pii_on_write(client):
    """PII 防线（服务端）：即便上游漏拦，也不得落库。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"

    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{GATEWAY_URL}/api/v1/internal/memory-service/memory/facts",
            headers={"X-Organization-Id": tenant_id, "X-Internal-Api-Key": INTERNAL_API_KEY},
            json={
                "user_id": user_id,
                "content": f"偏好：请记住我的手机号 {_PII_PHONE}",
                "category": "preference",
                "scope": "global",
                "entities": {"certainty": "confirmed"},
                "entity_key": "preference:联系方式",
                "source": {"producer": "e2e"},
            },
        )

    assert resp.status_code == 400, resp.text


async def _create_matter_and_sync_profile(*, user_id: int) -> str:
    if not INTERNAL_API_KEY:
        raise RuntimeError("INTERNAL_API_KEY is required for E2E materializer test")
    session_id = f"e2e-mem-materialize-{uuid.uuid4()}"
    async with httpx.AsyncClient(timeout=60.0) as c:
        created = await c.post(
            f"{GATEWAY_URL}/api/v1/internal/matter-service/matters/from-consultation",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json={
                "session_id": session_id,
                "user_id": str(int(user_id)),
                "title": "E2E Memory Materializer",
                "service_type_id": "civil_first_instance",
            },
        )
        created.raise_for_status()
        body = created.json()
        assert body.get("code") == 0, body
        data = body.get("data") or {}
        mid = str(data.get("id") or "").strip()
        assert mid, body

        # Sync minimal workflow profile: parties + intake_profile.facts (evidence hints)
        resp = await c.post(
            f"{GATEWAY_URL}/api/v1/internal/matter-service/matters/{mid}/sync/all",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json={
                "parties": [
                    {"role": "plaintiff", "role_order": 1, "name": f"张三E2E01，手机{_PII_PHONE}", "party_type": "person"},
                    {"role": "defendant", "role_order": 1, "name": "李四E2E01", "party_type": "person"},
                ],
                "intake_profile": {
                    "facts": "原告：张三E2E01。被告：李四E2E01。证据：借条、转账记录。",
                    "cause_of_action_name": "民间借贷纠纷",
                },
            },
        )
        resp.raise_for_status()
        ok = resp.json()
        assert ok.get("code") == 0, ok
        return mid


@pytest.mark.e2e
async def test_memory_materializer_builds_case_index_and_recallable(client):
    """route2：case 索引由 matter -> memory materializer 生成，且 recall 可命中。"""
    user_id = int(client.user_id)
    tenant_id = str(client.organization_id or "").strip()
    assert tenant_id, "missing client.organization_id (tenant)"
    matter_id = await _create_matter_and_sync_profile(user_id=user_id)

    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{AI_PLATFORM_URL}/api/v1/internal/ai/memory/materialize",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json={"user_id": user_id, "matter_id": matter_id, "cleanup_legacy": True, "tenant_id": tenant_id},
        )
        resp.raise_for_status()
        body = resp.json()
        assert body.get("code") == 0, body
        data = body.get("data") or {}
        assert (data.get("success") is True) or ("result" in data), data

    facts = await _list_case_facts_from_memory_service(user_id=user_id, tenant_id=tenant_id, case_id=matter_id, limit=200)
    keys = {str(it.get("entity_key") or "") for it in facts if isinstance(it, dict)}
    assert "party:plaintiff:primary" in keys
    assert "party:defendant:primary" in keys
    ev_iou = "evidence:hint:" + hashlib.md5("借条".encode("utf-8")).hexdigest()[:12]
    ev_tx = "evidence:hint:" + hashlib.md5("转账记录".encode("utf-8")).hexdigest()[:12]
    assert ev_iou in keys
    assert ev_tx in keys
    # PII must not be stored in derived index.
    for it in facts:
        if not isinstance(it, dict):
            continue
        assert _PII_PHONE not in str(it.get("entity_key") or "")
        assert _PII_PHONE not in str(it.get("content") or "")

    recalled = await _recall_from_memory_service(user_id=user_id, tenant_id=tenant_id, case_id=matter_id, query="借条", include_global=False)
    assert any((it.get("entity_key") == ev_iou) for it in (recalled.get("facts") or []) if isinstance(it, dict))
