from checkout import quote


def test_baseline_contract_rejects_experimental_rollout():
    assert quote(10) == {"total": 20, "label": "standard"}
