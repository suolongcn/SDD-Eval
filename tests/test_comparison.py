from sdd_eval.comparison import build_comparison_report
from sdd_eval.models import EvaluationResult, Prediction, TokenUsage
from fastapi.testclient import TestClient
from sdd_eval import api
from sdd_eval.models import BenchmarkInstance, EvaluationOracle
from sdd_eval.storage import Store
from sdd_eval.worker import BenchmarkWorker


def test_comparison_aggregates_models_and_efficiency():
    p1 = Prediction(instance_id="i1", model_name_or_path="glm-5.3", model_patch="a", token_usage=TokenUsage(input_tokens=10, output_tokens=5, latency_ms=100))
    p2 = Prediction(instance_id="i1", model_name_or_path="minimax-2.7", model_patch="b", token_usage=TokenUsage(input_tokens=20, output_tokens=10, latency_ms=200))
    r1 = EvaluationResult(prediction_id=p1.prediction_id, instance_id="i1", outcome="resolved", resolved=True, score=80, functional_score=90, code_quality_score=70, documentation_score=60)
    r2 = EvaluationResult(prediction_id=p2.prediction_id, instance_id="i1", outcome="unresolved", resolved=False, score=40, functional_score=40, code_quality_score=40, documentation_score=40)
    report = build_comparison_report([p1, p2], [r1, r2])
    assert report["total_runs"] == 2
    assert report["model_comparison"][0]["resolve_rate"] in (0, 1)
    assert report["model_comparison"][0]["avg_latency_ms"] > 0
    glm = next(row for row in report["model_comparison"] if row["model"] == "glm-5.3")
    assert glm["total_input_tokens"] == 10
    assert glm["total_output_tokens"] == 5
    assert glm["total_tokens"] == 15
    assert report["total_tokens"] == 45


def test_html_report_orders_models_by_average_score_descending():
    rows = [
        {"model": "low", "runs": 1, "resolved": 0, "resolve_rate": 0,
         "average_score": 20, "functional_score": 0, "code_quality_score": 0,
         "documentation_score": 0, "avg_latency_ms": 60000},
        {"model": "high", "runs": 1, "resolved": 1, "resolve_rate": 1,
         "average_score": 90, "functional_score": 100, "code_quality_score": 80,
         "documentation_score": 70, "avg_latency_ms": 90000},
    ]

    rendered = api._comparison_report_html("batch", {
        "model_comparison": rows, "instance_ids": [], "expected_runs": 0,
        "total_runs": 0, "instance_matrix": [], "details": [],
    })

    assert rendered.index("<b>high</b>") < rendered.index("<b>low</b>")
    assert "1.5 分钟" in rendered


def test_comparison_api_creates_cross_product_and_exposes_live_batch(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "comparison.db"))
    for instance_id in ("case-a", "case-b"):
        instance = BenchmarkInstance(instance_id=instance_id, repo="demo/repo", base_commit="abc", problem_statement="Fix it")
        store.put_benchmark_instance(instance, EvaluationOracle(instance_id=instance_id))
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)
    created = client.post("/api/comparisons", json={
        "instance_ids": ["case-a", "case-b"], "models": ["GLM5.3", "Minimax2.7"],
        "client": "opencode", "workflow": "codespec", "backend": "local",
    })
    assert created.status_code == 200
    payload = created.json()
    assert payload["count"] == 4
    assert payload["batch_id"].startswith("cmp-")
    assert set(payload["models"]) == {"gateway/glm-5.3", "gateway/minimax-2.7"}
    jobs = store.list_jobs()
    assert len(jobs) == 4 and all(job.batch_id == payload["batch_id"] for job in jobs)
    live = client.get("/api/comparisons/batches").json()[0]
    assert live["batch_id"] == payload["batch_id"]
    assert live["active"] == 4 and live["completed"] == 0
    report = client.get("/api/comparisons/report", params={"batch_id": payload["batch_id"]}).json()
    assert report["batch_id"] == payload["batch_id"]
    assert report["total_runs"] == 0
    assert report["expected_runs"] == 4
    assert len(report["instance_matrix"]) == 4
    assert all(item["status"] == "queued" for item in report["instance_matrix"])
    assert all(item["client"] == "opencode" and item["workflow"] == "codespec" for item in report["instance_matrix"])

    html_report = client.get(f"/api/comparisons/{payload['batch_id']}/report.html")
    assert html_report.status_code == 200
    assert "模型整体表现" in html_report.text
    assert "测试用例 × 模型表现矩阵" in html_report.text
    assert "编码工具" in html_report.text and "SDD 工具" in html_report.text
    csv_report = client.get("/api/comparisons/report.csv", params={"batch_id": payload["batch_id"]})
    assert csv_report.status_code == 200
    assert "gateway/glm-5.3" in csv_report.text and "average_score" in csv_report.text
    assert "total_tokens" in csv_report.text


class _BatchGenerator:
    def generate(self, instance, client, model, workflow, workspace=None):
        return Prediction(instance_id=instance.instance_id, model_name_or_path=model,
                          client=client, workflow=workflow, model_patch="patch")


class _BatchBackend:
    def evaluate(self, instance, oracle, prediction, workspace=None):
        return EvaluationResult(prediction_id=prediction.prediction_id, instance_id=instance.instance_id,
                                outcome="resolved", resolved=True, score=100)


def test_batch_jobs_flow_into_completed_report(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "worker.db"))
    instance = BenchmarkInstance(instance_id="case", repo="demo/repo", base_commit="abc", problem_statement="Fix it")
    store.put_benchmark_instance(instance, EvaluationOracle(instance_id="case"))
    monkeypatch.setattr(api, "store", store)
    created = TestClient(api.app).post("/api/comparisons", json={
        "instance_ids": ["case"], "models": ["GLM5.3", "Minimax2.7"], "client": "opencode",
        "workflow": "codespec", "backend": "local",
    }).json()
    worker = BenchmarkWorker(store, worker_id="batch-worker", backend_factory=lambda _: _BatchBackend(), generator_factory=_BatchGenerator)
    assert worker.run_once() and worker.run_once()
    live = TestClient(api.app).get("/api/comparisons/batches").json()[0]
    assert live["completed"] == 2 and live["active"] == 0
    report = TestClient(api.app).get("/api/comparisons/report", params={"batch_id": created["batch_id"]}).json()
    assert report["total_runs"] == 2
    assert {row["model"] for row in report["model_comparison"]} == {"gateway/glm-5.3", "gateway/minimax-2.7"}
    assert all(item["status"] == "completed" for item in report["instance_matrix"])
    assert all(item["client"] == "opencode" and item["workflow"] == "codespec" for item in report["instance_matrix"])

    # Reports are reconstructed from persisted records after a process restart.
    reopened = Store(str(tmp_path / "worker.db"))
    monkeypatch.setattr(api, "store", reopened)
    restored = TestClient(api.app).get("/api/comparisons/report", params={"batch_id": created["batch_id"]})
    assert restored.status_code == 200 and restored.json()["total_runs"] == 2
    assert TestClient(api.app).get(f"/api/comparisons/{created['batch_id']}/report.html").status_code == 200
