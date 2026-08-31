from sdd_eval.models import TaskSpec, ComparisonResult, RunResult
from sdd_eval.models import TokenUsage
from sdd_eval.evaluator import evaluate
from sdd_eval.adapters import OpenSpecAdapter
from sdd_eval.models import compose_client_model
from sdd_eval.storage import Store
from types import SimpleNamespace
from pathlib import Path
import re
import shutil


def test_openai_provider_retries_transient_disconnect(monkeypatch):
    from sdd_eval.providers import OpenAICompatibleProvider

    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        def close(self):
            return None

    def fake_post(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            import httpx
            raise httpx.RemoteProtocolError("server disconnected")
        return Response()

    monkeypatch.setattr("sdd_eval.providers.httpx.post", fake_post)
    monkeypatch.setattr("sdd_eval.providers.time.sleep", lambda _: None)
    monkeypatch.setenv("SDD_EVAL_PROVIDER_RETRIES", "2")

    text, usage = OpenAICompatibleProvider("http://model", model="demo").complete("hello")
    assert text == "ok"
    assert usage.provider == "demo"
    assert len(calls) == 2


def test_openspec_provider_failure_falls_back_to_cli(tmp_path, monkeypatch):
    class BrokenProvider:
        simulation = False

        def complete(self, prompt):
            raise RuntimeError("gateway disconnected")

    def fake_cli(self, task, workspace, model, client):
        spec = workspace / "openspec"
        spec.mkdir()
        code = workspace / "generated_code.md"
        code.write_text("# Generated Code\n", encoding="utf-8")
        return spec, TokenUsage(provider="codex-cli", mode="model"), {"code": str(code)}, {
            "mode": "model",
            "response_parsed": True,
            "files_applied": ["src/generated.py"],
            "implementation_applied": True,
        }

    monkeypatch.setattr("sdd_eval.adapters.provider_for", lambda _: BrokenProvider())
    monkeypatch.setattr(OpenSpecAdapter, "_run_cli", fake_cli)
    task = TaskSpec(id="fallback", title="demo", build_command="")

    _, _, _, generation = OpenSpecAdapter().run(task, tmp_path, "http://model")
    assert generation["fallback"] == "codex-cli"
    assert "gateway disconnected" in generation["provider_error"]


def test_mock_evaluation(tmp_path):
    result=evaluate(TaskSpec(id="t",title="demo",build_command=""),"openspec","mock",str(tmp_path)); assert result.status=="incomplete"; assert result.score is not None

def test_superpowers_mock_workflow(tmp_path):
    result = evaluate(TaskSpec(id="sp", title="demo", build_command=""), "superpowers", "mock", str(tmp_path))
    assert result.status == "incomplete"
    assert set(result.artifacts["documents"]) == {"spec.md", "plan.md", "tasks.md"}
    assert result.metrics["generation"]["workflow"] == "superpowers"

def test_opencode_cli_is_selected_for_opencode_model(tmp_path, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "opencode.cmd":
            (tmp_path / "src").mkdir()
            (tmp_path / "src" / "generated.py").write_text("pass")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("sdd_eval.adapters.subprocess.run", fake_run)
    task = TaskSpec(id="op", title="demo", build_command="")
    _, _, _, generation = OpenSpecAdapter().run(task, tmp_path, "opencode")
    assert any(c[0] == "opencode.cmd" and c[1] == "run" for c in calls)
    assert any("--tools" in c and "opencode" in c for c in calls)
    assert generation["client"] == "opencode"

def test_opencode_receives_selected_model(tmp_path, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        (tmp_path / "generated.py").write_text("pass")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("sdd_eval.adapters.subprocess.run", fake_run)
    task = TaskSpec(id="op-model", title="demo", build_command="")
    OpenSpecAdapter().run(task, tmp_path, "opencode:gpt-5.6-luna")
    cli_call = next(c for c in calls if c[0] == "opencode.cmd")
    assert cli_call[cli_call.index("--model") + 1] == "gpt-5.6-luna"

def test_superpowers_accepts_opencode_cli(tmp_path, monkeypatch):
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        (tmp_path / "generated.py").write_text("pass")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("sdd_eval.adapters.subprocess.run", fake_run)
    task = TaskSpec(id="sp-op", title="demo", build_command="")
    _, _, paths, generation = __import__("sdd_eval.adapters", fromlist=["SuperpowersAdapter"]).SuperpowersAdapter().run(task, tmp_path, "opencode")
    assert calls[0][0] == "opencode.cmd"
    assert generation["workflow"] == "superpowers"
    assert "spec.md" in paths

def test_client_and_model_are_composed_separately():
    assert compose_client_model("codex", "gpt-5.6-luna") == "codex:gpt-5.6-luna"
    assert compose_client_model("opencode", "terra") == "opencode:gpt-5.6-terra"
    assert compose_client_model("codex", "opencode") == "opencode"

def test_dashboard_has_independent_client_model_selectors():
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    assert 'id="client"' in html and 'id="model"' in html
    assert "Codex CLI" in html and "OpenCode CLI" in html
    assert 'value="gpt-5.6-luna">Luna' in html

def test_runs_history_tab_follows_model_compare():
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    model_compare = re.search(r'<button[^>]+data-view="model-compare"[^>]*>\s*Model Compare\s*</button\s*>', html)
    runs_history = re.search(r'<button[^>]+data-view="runs"[^>]*>\s*Runs History\s*</button\s*>', html)
    assert model_compare and runs_history
    assert model_compare.start() < runs_history.start()

def test_comparison_history_is_persisted_and_summarized(tmp_path, monkeypatch):
    from sdd_eval import api
    comparison_store = Store(str(tmp_path / "comparisons.db"))
    pending = RunResult(run_id="run-1", task_id="task-1", status="running")
    comparison_store.put_run(pending)
    monkeypatch.setattr(api, "store", comparison_store)
    comparison = ComparisonResult(
        comparison_id="comparison-1",
        task_ids=["task-1"],
        models=["gpt-5.6-luna"],
        run_ids=["run-1"],
    )
    comparison_store.put_comparison(comparison)

    summary = api.comparison_detail("comparison-1")

    assert summary["status"] == "running"
    assert summary["completed_runs"] == 0
    assert summary["total_runs"] == 1
    assert summary["started_at"]

def test_dashboard_renders_persistent_comparison_history():
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    assert 'id="comparisonHistory"' in html
    assert 'fetch("/api/comparisons")' in html
    assert "View report" in html and "Average score" in html

def test_dashboard_inline_script_has_valid_javascript():
    if shutil.which("node") is None:
        return
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]*?)</script>", html).group(1)
    import subprocess
    result = subprocess.run(
        ["node", "-e", "new Function(process.argv[1])", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

def test_task_created_at_is_archived_and_rendered(tmp_path):
    store = Store(str(tmp_path / "created-at.db"))
    task = TaskSpec(id="created", title="created task", build_command="")
    store.put_task(task)
    loaded = store.get_task("created")
    assert loaded.created_at is not None
    assert loaded.created_at.tzinfo is not None
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    assert "<th>Created</th>" in html and "t.created_at" in html
