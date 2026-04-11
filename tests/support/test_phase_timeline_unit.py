from __future__ import annotations

from support.workbench.phase_timeline import assert_has_deliverable, deliverable_output_keys, deliverables


def test_deliverables_reads_hard_cut_deliverables_field() -> None:
    payload = {
        "matterId": "42",
        "phases": [{"id": "authority_resolution", "status": "awaiting_review", "current": True}],
        "deliverables": [{"outputKey": "contract_review_report", "fileId": "file-1", "status": "published"}],
    }

    rows = deliverables(payload)

    assert rows == [{"outputKey": "contract_review_report", "fileId": "file-1", "status": "published"}]
    assert deliverable_output_keys(payload) == {"contract_review_report"}


def test_assert_has_deliverable_requires_deliverables_contract() -> None:
    payload = {
        "matterId": "42",
        "phases": [{"id": "authority_resolution", "status": "awaiting_review", "current": True}],
        "deliverables": [{"outputKey": "contract_review_report", "fileId": "file-1", "status": "published"}],
    }

    assert_has_deliverable(payload, output_key="contract_review_report")
