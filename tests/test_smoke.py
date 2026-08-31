from sdd_eval.models import TaskSpec
from sdd_eval.models import TokenUsage
from sdd_eval.evaluator import evaluate
from sdd_eval.adapters import OpenSpecAdapter
from sdd_eval.models import compose_client_model
from sdd_eval.storage import Store
from types import SimpleNamespace
from pathlib import Path


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

def test_task_created_at_is_archived_and_rendered(tmp_path):
    store = Store(str(tmp_path / "created-at.db"))
    task = TaskSpec(id="created", title="created task", build_command="")
    store.put_task(task)
    loaded = store.get_task("created")
    assert loaded.created_at is not None
    assert loaded.created_at.tzinfo is not None
    html = Path(__file__).parents[1].joinpath("sdd_eval", "dashboard.html").read_text(encoding="utf-8")
    assert "<th>Created</th>" in html and "t.created_at" in html
