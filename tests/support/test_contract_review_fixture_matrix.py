from __future__ import annotations

import json
from pathlib import Path


CONTRACT_TYPE_IDS = (
    "construction",
    "sales",
    "service",
    "labor",
    "lease",
    "loan",
    "technology",
    "equity_transfer",
    "franchise",
    "other",
)


def test_contract_review_fixture_matrix_covers_all_contract_types() -> None:
    root = Path(__file__).resolve().parents[2] / "fixtures" / "workbench" / "contract_review"

    for contract_type_id in CONTRACT_TYPE_IDS:
        fixture_path = root / f"{contract_type_id}.txt"
        expectation_path = root / f"{contract_type_id}.expectation.json"

        assert fixture_path.is_file(), contract_type_id
        assert expectation_path.is_file(), contract_type_id

        payload = json.loads(expectation_path.read_text(encoding="utf-8"))
        assert payload["contract_type_id"] == contract_type_id
        assert payload["review_scope"] == "full"
        assert payload["required_output_keys"] == ["contract_review_report"]
