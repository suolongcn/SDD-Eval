from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient

from sdd_eval import api
from sdd_eval.models import (
    BenchmarkInstance, BenchmarkJob, EvaluationOracle, EvaluationResult,
    GenerationJobCreate, InstanceValidationResult, Prediction, now,
)
from sdd_eval.storage import Store
from sdd_eval.worker import BenchmarkWorker


def prepared_store(tmp_path):
    store = Store(str(tmp_path / "jobs.db"))
    instance = BenchmarkInstance(instance_id="demo__job-1", repo="demo/repo", base_commit="abc", problem_statement="Fix it")
    oracle = EvaluationOracle(instance_id=instance.instance_id)
    prediction = Prediction(prediction_id="prediction-job", instance_id=instance.instance_id,
                            model_name_or_path="demo", model_patch="diff --git a/a b/a\n")
    store.put_benchmark_instance(instance); store.put_evaluation_oracle(oracle); store.put_prediction(prediction)
    return store, instance, prediction


def test_claim_is_atomic_across_workers(tmp_path):
    store, instance, prediction = prepared_store(tmp_path)
    store.put_job(BenchmarkJob(job_id="job-atomic", kind="evaluate_prediction", instance_id=instance.instance_id,
                               prediction_id=prediction.prediction_id, backend="local"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda worker: Store(store.path).claim_job(worker), ["worker-a", "worker-b"]))
    assert sum(item is not None for item in claimed) == 1
    assert store.get_job("job-atomic").attempt == 1
    assert len(store.list_job_attempts("job-atomic")) == 1


def test_expired_lease_is_recovered_and_retried(tmp_path):
    store, instance, _ = prepared_store(tmp_path)
    store.put_job(BenchmarkJob(job_id="job-stale", kind="validate_instance", instance_id=instance.instance_id, backend="local"))
    claimed = store.claim_job("dead-worker", lease_seconds=30)
    claimed.lease_expires_at = now() - timedelta(seconds=1)
    with store.conn() as connection:
        connection.execute("update benchmark_jobs set lease_expires_at=?, data=? where id=?",
                           (claimed.lease_expires_at.isoformat(), claimed.model_dump_json(), claimed.job_id))
    recovered = store.claim_job("new-worker")
    assert recovered.job_id == claimed.job_id
    assert recovered.attempt == 2
    assert [item.status for item in store.list_job_attempts(claimed.job_id)] == ["expired", "running"]


def test_cancellation_and_explicit_retry(tmp_path):
    store, instance, _ = prepared_store(tmp_path)
    queued = BenchmarkJob(job_id="job-cancel", kind="validate_instance", instance_id=instance.instance_id)
    store.put_job(queued)
    assert store.request_job_cancellation(queued.job_id).status == "cancelled"
    retried = store.retry_job(queued.job_id)
    assert retried.status == "queued" and not retried.cancellation_requested


def test_unresolved_completed_generation_job_can_be_retried(tmp_path):
    store, instance, prediction = prepared_store(tmp_path)
    result = EvaluationResult(prediction_id=prediction.prediction_id, instance_id=instance.instance_id,
                              outcome="target_tests_failed")
    store.put_evaluation_result(result)
    job = BenchmarkJob(job_id="job-unresolved", kind="generate_and_evaluate", instance_id=instance.instance_id,
                       prediction_id=prediction.prediction_id, backend="local", client="codex",
                       model="gpt-5.6-luna", workflow="openspec", status="completed", result_id=result.evaluation_id)
    store.put_job(job)

    retried = store.retry_job(job.job_id, allow_completed=True)

    assert retried.status == "queued"
    assert retried.prediction_id is None and retried.result_id is None


class SuccessfulBackend:
    def validate_instance(self, instance, oracle, workspace=None):
        return InstanceValidationResult(instance_id=instance.instance_id, valid=True)

    def evaluate(self, instance, oracle, prediction, workspace=None):
        return EvaluationResult(prediction_id=prediction.prediction_id, instance_id=instance.instance_id,
                                  outcome="resolved", resolved=True, score=100, prediction_hash=prediction.patch_hash)


class SuccessfulGenerator:
    def generate(self, instance, client, model, workflow, workspace=None):
        return Prediction(instance_id=instance.instance_id, model_name_or_path=model, client=client,
                          workflow=workflow, model_patch="diff --git a/a b/a\n")


def test_worker_persists_result_and_attempt(tmp_path):
    store, instance, prediction = prepared_store(tmp_path)
    job = BenchmarkJob(job_id="job-worker", kind="evaluate_prediction", instance_id=instance.instance_id,
                       prediction_id=prediction.prediction_id, backend="local")
    store.put_job(job)
    worker = BenchmarkWorker(store, worker_id="worker", backend_factory=lambda _: SuccessfulBackend())
    assert worker.run_once()
    completed = store.get_job(job.job_id)
    assert completed.status == "completed"
    assert store.get_evaluation_result(completed.result_id).resolved
    assert store.list_job_attempts(job.job_id)[0].status == "completed"


def test_worker_generates_prediction_then_evaluates_it(tmp_path):
    store, instance, _ = prepared_store(tmp_path)
    job = BenchmarkJob(job_id="job-agent", kind="generate_and_evaluate", instance_id=instance.instance_id,
                       backend="local", client="codex", model="gpt-5.6-terra", workflow="openspec")
    store.put_job(job)
    worker = BenchmarkWorker(store, worker_id="worker", backend_factory=lambda _: SuccessfulBackend(),
                             generator_factory=SuccessfulGenerator)

    assert worker.run_once()

    completed = store.get_job(job.job_id)
    prediction = store.get_prediction(completed.prediction_id)
    result = store.get_evaluation_result(completed.result_id)
    assert completed.status == "completed"
    assert (prediction.client, prediction.model_name_or_path, prediction.workflow) == ("codex", "gpt-5.6-terra", "openspec")
    assert result.score == 100


def test_validation_job_links_to_the_validation_record(tmp_path):
    store, instance, _ = prepared_store(tmp_path)
    job = BenchmarkJob(job_id="job-validation", kind="validate_instance", instance_id=instance.instance_id, backend="local")
    store.put_job(job)
    BenchmarkWorker(store, worker_id="worker", backend_factory=lambda _: SuccessfulBackend()).run_once()
    completed = store.get_job(job.job_id)
    validation = store.get_instance_validation(instance.instance_id)
    assert completed.status == "completed"
    assert completed.result_id == validation.validation_id


def test_job_api_validates_prediction_ownership_and_never_exposes_oracle(tmp_path, monkeypatch):
    store, instance, prediction = prepared_store(tmp_path)
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)
    response = client.post("/api/jobs", json={"kind": "evaluate_prediction", "instance_id": instance.instance_id,
                                                         "prediction_id": prediction.prediction_id, "backend": "docker"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    assert client.get(f"/api/jobs/{job_id}/attempts").json() == []
    assert client.get(f"/api/evaluation-oracles/{instance.instance_id}").status_code == 404


def test_http_job_rejects_the_unisolated_local_backend(tmp_path, monkeypatch):
    store, instance, _ = prepared_store(tmp_path); monkeypatch.setattr(api, "store", store)
    response = TestClient(api.app).post("/api/jobs", json={"kind": "validate_instance", "instance_id": instance.instance_id, "backend": "local"})
    assert response.status_code == 400
    assert "docker" in response.json()["detail"]


def test_generation_api_queues_selected_agent_combination(tmp_path, monkeypatch):
    store, instance, _ = prepared_store(tmp_path); monkeypatch.setattr(api, "store", store)
    response = TestClient(api.app).post("/api/generations", json={
        "instance_id": instance.instance_id,
        "client": "opencode",
        "model": "gpt-5.6-luna",
        "workflow": "superpowers",
        "backend": "local",
    })
    assert response.status_code == 200
    job = response.json()
    assert job["kind"] == "generate_and_evaluate"
    assert (job["client"], job["model"], job["workflow"]) == ("opencode", "gpt-5.6-luna", "superpowers")
