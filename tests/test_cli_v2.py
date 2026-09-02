import json

from typer.testing import CliRunner

from sdd_eval.cli import app
from sdd_eval.models import BenchmarkInstance, EvaluationOracle
from sdd_eval.storage import Store


runner = CliRunner()


def test_cli_exposes_only_v2_workflow_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("import-dataset", "import-predictions", "validate-instance", "evaluate", "enqueue", "worker"):
        assert command in result.output
    assert "import-task" not in result.output


def test_prediction_jsonl_import(tmp_path):
    database = str(tmp_path / "cli.db"); store = Store(database)
    instance = BenchmarkInstance(instance_id="demo__cli-1", repo="demo/repo", base_commit="abc", problem_statement="Fix it")
    store.put_benchmark_instance(instance, EvaluationOracle(instance_id=instance.instance_id))
    source = tmp_path / "predictions.jsonl"
    source.write_text(json.dumps({"instance_id": instance.instance_id, "model_name_or_path": "agent", "model_patch": "patch"}) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["import-predictions", str(source), "--db", database])
    assert result.exit_code == 0
    assert len(store.list_predictions()) == 1
