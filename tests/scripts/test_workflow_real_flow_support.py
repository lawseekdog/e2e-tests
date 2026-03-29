from __future__ import annotations

from scripts._support.workflow_real_flow_support import configure_direct_service_mode


def test_configure_direct_service_mode_uses_lane_local_ports(monkeypatch) -> None:
    monkeypatch.delenv("E2E_CONSULTATIONS_BASE_URL", raising=False)
    monkeypatch.delenv("E2E_MATTER_BASE_URL", raising=False)
    monkeypatch.delenv("E2E_TEMPLATES_BASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_CONSULTATIONS_PORT", "18027")
    monkeypatch.setenv("LOCAL_MATTER_PORT", "18026")
    monkeypatch.setenv("LOCAL_TEMPLATES_PORT", "18025")

    base_url, config = configure_direct_service_mode(
        remote_stack_host="100.116.203.71",
        local_consultations=True,
        local_matter=True,
        local_templates=True,
    )

    assert base_url == "http://127.0.0.1:18027/api/v1"
    assert config["consultations_base_url"] == "http://127.0.0.1:18027/api/v1"
    assert config["matter_base_url"] == "http://127.0.0.1:18026/api/v1"
    assert config["templates_base_url"] == "http://127.0.0.1:18025/api/v1"
