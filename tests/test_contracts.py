import hashlib
import json

import pytest

from sdd_eval.contracts import contract_digest, cucumber_scenarios, validate_adapter_patch
from sdd_eval.harness import CheckoutResult, CommandResult, LocalEvaluationBackend
from sdd_eval.models import CucumberContractSpec, EvaluationOracle, TestAdapterSpec as AdapterSpec


def report_payload(status="passed"):
    return [{"name": "Account API", "elements": [{"name": "Create account", "type": "scenario",
        "tags": [{"name": "@target"}, {"name": "@http"}],
        "steps": [{"keyword": "Given ", "name": "a request", "result": {"status": status}}]}]}]


def test_cucumber_report_filters_tags_and_requires_every_step_to_pass(tmp_path):
    report = tmp_path / "cucumber.json"
    report.write_text(json.dumps(report_payload()), encoding="utf-8")
    scenarios = cucumber_scenarios(report, ["target"])
    assert scenarios[0]["passed"] is True
    assert scenarios[0]["framework"] == "cucumber"
    report.write_text(json.dumps(report_payload("failed")), encoding="utf-8")
    assert cucumber_scenarios(report, ["@target"])[0]["passed"] is False
    assert cucumber_scenarios(report, ["@regression"]) == []


def test_cucumber_report_rejects_missing_or_malformed_json(tmp_path):
    with pytest.raises(ValueError, match="invalid Cucumber JSON"):
        cucumber_scenarios(tmp_path / "missing.json", ["@target"])
    report = tmp_path / "bad.json"; report.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Cucumber JSON"):
        cucumber_scenarios(report, ["@target"])


def test_adapter_patch_is_confined_to_binding_paths_and_contract_digest_is_stable(tmp_path):
    (tmp_path / "features").mkdir(); (tmp_path / "bindings").mkdir()
    feature = tmp_path / "features" / "account.feature"; feature.write_text("Feature: account", encoding="utf-8")
    digest = contract_digest(tmp_path, ["features/account.feature"])
    expected = hashlib.sha256(b"features/account.feature\0Feature: account\0").hexdigest()
    assert digest == expected
    valid_patch = "diff --git a/bindings/http.py b/bindings/http.py\n--- a/bindings/http.py\n+++ b/bindings/http.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert validate_adapter_patch(valid_patch, ["bindings/http.py"])[0]
    ok, message = validate_adapter_patch(valid_patch.replace("bindings/http.py", "features/account.feature"), ["bindings/http.py"])
    assert not ok and "protected" in message
    with pytest.raises(ValueError, match="missing"):
        contract_digest(tmp_path, ["missing.feature"])


def test_oracle_accepts_frozen_cucumber_contract_and_adapter():
    oracle = EvaluationOracle(instance_id="case", oracle_kind="behavioral_contract",
        cucumber=CucumberContractSpec(report_path="reports/cucumber.json"),
        test_adapter=AdapterSpec(patch="", allowed_paths=["bindings/http.py"],
            frozen_contract_paths=["features/account.feature"], frozen_contract_digest="a" * 64))
    assert oracle.cucumber.target_tags == ["@target"]
    assert oracle.test_adapter.allowed_paths == ["bindings/http.py"]


def test_harness_applies_only_a_digest_verified_adapter(tmp_path, monkeypatch):
    (tmp_path / "features").mkdir(); (tmp_path / "bindings").mkdir()
    (tmp_path / "features" / "account.feature").write_text("Feature: account", encoding="utf-8")
    patch = "diff --git a/bindings/http.py b/bindings/http.py\n--- a/bindings/http.py\n+++ b/bindings/http.py\n@@ -0,0 +1 @@\n+x = 1\n"
    oracle = EvaluationOracle(instance_id="case", test_patch="private tests", test_adapter=AdapterSpec(
        patch=patch, allowed_paths=["bindings/http.py"], frozen_contract_paths=["features/account.feature"],
        frozen_contract_digest=contract_digest(tmp_path, ["features/account.feature"])))
    backend = LocalEvaluationBackend(); applied = []
    monkeypatch.setattr(backend, "_apply_patch", lambda root, value, label: applied.append(label) or CommandResult(True, 0, label))
    result = CheckoutResult()
    assert backend._apply_test_assets(tmp_path, oracle, result)
    assert applied == ["test patch", "test adapter patch"]
    oracle.test_adapter.frozen_contract_digest = "0" * 64
    rejected = CheckoutResult()
    assert not backend._apply_test_assets(tmp_path, oracle, rejected)
    assert rejected.error_kind == "harness_error"
