import os
import socket
import threading
import time
import uuid
from collections.abc import Callable

from .docker_backend import DockerEvaluationBackend
from .harness import LocalEvaluationBackend
from .generator import AgentGenerator
from .storage import Store


def create_backend(name: str):
    if name == "local":
        return LocalEvaluationBackend()
    if name == "docker":
        return DockerEvaluationBackend()
    raise ValueError(f"unsupported backend: {name}")


class BenchmarkWorker:
    """Consumes durable benchmark jobs. Cancellation is cooperative at backend boundaries."""

    def __init__(self, store: Store, worker_id: str | None = None, lease_seconds: int = 60,
                 backend_factory: Callable[[str], object] = create_backend,
                 generator_factory: Callable[[], AgentGenerator] = AgentGenerator):
        self.store = store
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_seconds = lease_seconds
        self.backend_factory = backend_factory
        self.generator_factory = generator_factory

    def run_once(self) -> bool:
        job = self.store.claim_job(self.worker_id, self.lease_seconds)
        if not job:
            return False
        stopped = threading.Event()

        def maintain_lease():
            interval = max(1, self.lease_seconds // 3)
            while not stopped.wait(interval):
                if not self.store.heartbeat_job(job.job_id, self.worker_id, self.lease_seconds):
                    return

        heartbeat = threading.Thread(target=maintain_lease, daemon=True)
        heartbeat.start()
        try:
            instance = self.store.get_benchmark_instance(job.instance_id)
            oracle = self.store.get_evaluation_oracle(job.instance_id)
            if not instance or not oracle:
                raise ValueError("benchmark instance or private oracle not found")
            if job.kind == "validate_instance":
                if not self.store.heartbeat_job(job.job_id, self.worker_id, self.lease_seconds, "evaluating"):
                    self.store.finish_job(job.job_id, self.worker_id, error="job cancelled before evaluation")
                    return True
                backend = self.backend_factory(job.backend)
                result = backend.validate_instance(instance, oracle, workspace=job.workspace)
                self.store.put_instance_validation(result)
                result_id = result.validation_id
            else:
                if job.kind == "generate_and_evaluate":
                    if not self.store.heartbeat_job(job.job_id, self.worker_id, self.lease_seconds, "generating"):
                        self.store.finish_job(job.job_id, self.worker_id, error="job cancelled before generation")
                        return True
                    prediction = self.generator_factory().generate(
                        instance, job.client, job.model, job.workflow, workspace=job.workspace,
                    )
                    self.store.put_prediction(prediction)
                    attached = self.store.attach_job_prediction(job.job_id, self.worker_id, prediction.prediction_id)
                    if not attached:
                        self.store.finish_job(job.job_id, self.worker_id, error="job cancelled after generation")
                        return True
                    job.prediction_id = prediction.prediction_id
                else:
                    prediction = self.store.get_prediction(job.prediction_id)
                if not prediction:
                    raise ValueError("prediction not found")
                if not self.store.heartbeat_job(job.job_id, self.worker_id, self.lease_seconds, "evaluating"):
                    self.store.finish_job(job.job_id, self.worker_id, error="job cancelled before evaluation")
                    return True
                backend = self.backend_factory(job.backend)
                result = backend.evaluate(instance, oracle, prediction, workspace=job.workspace)
                self.store.put_evaluation_result(result)
                result_id = result.evaluation_id
            self.store.finish_job(job.job_id, self.worker_id, result_id=result_id)
        except Exception as error:
            self.store.finish_job(job.job_id, self.worker_id, error=str(error), retry_delay_seconds=min(60, 2 ** job.attempt))
        finally:
            stopped.set()
            heartbeat.join(timeout=1)
        return True

    def run_forever(self, poll_seconds: float = 1.0, stop_event: threading.Event | None = None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            if not self.run_once():
                stop_event.wait(poll_seconds)


def run_workers(db: str, concurrency: int, lease_seconds: int, poll_seconds: float, once: bool):
    threads = []
    for index in range(concurrency):
        worker = BenchmarkWorker(Store(db), worker_id=f"{socket.gethostname()}-{os.getpid()}-{index}", lease_seconds=lease_seconds)
        target = worker.run_once if once else lambda item=worker: item.run_forever(poll_seconds)
        thread = threading.Thread(target=target, name=f"benchmark-worker-{index}")
        thread.start(); threads.append(thread)
    for thread in threads:
        thread.join()
