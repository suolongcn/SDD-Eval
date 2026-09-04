from pathlib import Path
import uuid
import csv
import io
import html
import shutil
import subprocess
import time
import httpx

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from .models import BenchmarkInstance, BenchmarkJob, BenchmarkJobCreate, GenerationJobCreate, Prediction, ComparisonRequest, ComparisonReportRequest
from .comparison import build_comparison_report
from .generator import AgentGenerator
from .storage import Store
from .pr_sources import Forge, PullRequestImport, PullRequestSourceService, SizeRange


app = FastAPI(title="SDD Eval", version="2.0.0")
store = Store()
pr_source_service = PullRequestSourceService()
_model_cache: tuple[float, list[str]] = (0.0, [])


def _opencode_models() -> list[str]:
    """Return model identifiers advertised by the installed OpenCode CLI."""
    global _model_cache
    cached_at, cached = _model_cache
    if cached and time.monotonic() - cached_at < 60:
        return cached
    executable = shutil.which("opencode") or shutil.which("opencode.cmd")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cached
    models = sorted({line.strip() for line in result.stdout.splitlines() if "/" in line and " " not in line.strip()}) if result.returncode == 0 else []
    if models:
        _model_cache = (time.monotonic(), models)
        return models
    return cached


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()] or None


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(
        (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/dashboard.js", response_class=PlainTextResponse, include_in_schema=False)
def dashboard_script():
    return PlainTextResponse(
        (Path(__file__).parent / "dashboard.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/summary")
def summary():
    return store.dashboard_summary()


@app.get("/api/generation-capabilities")
def generation_capabilities():
    opencode_models = _opencode_models()
    fallback_models = ["gateway/glm-5.3", "gateway/glm-5.3-flash", "gateway/minimax-2.7"]
    gateway_models = [model for model in opencode_models if model.startswith("gateway/")]
    return {
        "clients": [{"id": name, "available": bool(shutil.which(name) or shutil.which(f"{name}.cmd"))} for name in ("codex", "opencode")],
        "models": opencode_models or fallback_models,
        "opencode_models": opencode_models or fallback_models,
        "gateway_models": gateway_models or fallback_models,
        "models_source": "opencode" if opencode_models else "fallback",
        "workflows": [{"id": "openspec", "available": bool(shutil.which("openspec") or shutil.which("openspec.cmd"))}, {"id": "codespec", "available": True}, {"id": "superpowers", "available": True}],
    }


@app.get("/api/instances")
def instances(dataset_id: str | None = None, split: str | None = None):
    return store.list_benchmark_instances(dataset_id=dataset_id, split=split)


@app.get("/api/instances/{instance_id}")
def instance(instance_id: str):
    value = store.get_benchmark_instance(instance_id)
    if not value: raise HTTPException(404, "benchmark instance not found")
    return value


@app.get("/api/instance-test-summaries")
def instance_test_summaries():
    """Expose execution statistics without revealing private test selectors."""
    summaries = []
    for value in store.list_benchmark_instances():
        results = store.list_evaluation_results(instance_id=value.instance_id)
        validation = store.get_instance_validation(value.instance_id)
        latest = results[0] if results else None
        summaries.append({
            "instance_id": value.instance_id,
            "evaluation_count": len(results),
            "latest_evaluation_id": latest.evaluation_id if latest else None,
            "fail_to_pass": {
                "passed": latest.fail_to_pass_passed,
                "total": latest.fail_to_pass_total,
            } if latest else None,
            "pass_to_pass": {
                "passed": latest.pass_to_pass_passed,
                "total": latest.pass_to_pass_total,
            } if latest else None,
            "validation": {
                "valid": validation.valid,
                "validation_id": validation.validation_id,
            } if validation else None,
        })
    return summaries


@app.post("/api/instances")
def create_instance(value: BenchmarkInstance):
    if value.docker.build_context or value.docker.dockerfile or value.docker.pull:
        raise HTTPException(400, "image build and pull settings are administrator-only")
    store.put_benchmark_instance(value)
    return value


@app.delete("/api/instances/{instance_id}")
def delete_instance(instance_id: str):
    if not store.delete_benchmark_instance(instance_id): raise HTTPException(404, "benchmark instance not found")
    return {"deleted": instance_id}


@app.get("/api/pr-sources/repositories")
def search_source_repositories(
    forge: Forge, name: str = "", language: str = "", limit: int = Query(default=20, ge=1, le=50),
):
    """Search public repositories without exposing provider credentials."""
    try:
        return pr_source_service.search_repositories(forge, name, language, limit)
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(502 if not isinstance(exc, ValueError) else 400, str(exc)) from exc


@app.get("/api/pr-sources/pulls")
def search_source_pull_requests(
    forge: Forge, repository: str, size: SizeRange = "all",
    limit: int = Query(default=30, ge=1, le=100),
):
    """List merged PRs and filter by their exact additions + deletions."""
    try:
        return pr_source_service.list_pull_requests(forge, repository, size, limit)
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(502 if not isinstance(exc, ValueError) else 400, str(exc)) from exc


@app.post("/api/pr-sources/import")
def import_source_pull_request(request: PullRequestImport):
    """Create a public benchmark and private executable Oracle atomically."""
    try:
        instance, oracle = pr_source_service.import_pull_request(request)
        if store.get_benchmark_instance(instance.instance_id):
            raise HTTPException(409, "this pull request has already been imported")
        store.put_benchmark_instance(instance, oracle)
        return instance
    except HTTPException:
        raise
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        raise HTTPException(502 if not isinstance(exc, ValueError) else 400, str(exc)) from exc


@app.get("/api/predictions")
def predictions(instance_id: str | None = None):
    return store.list_predictions(instance_id=instance_id)


@app.get("/api/predictions/{prediction_id}")
def prediction(prediction_id: str):
    value = store.get_prediction(prediction_id)
    if not value: raise HTTPException(404, "prediction not found")
    return value


@app.post("/api/predictions")
def create_prediction(value: Prediction):
    if not store.get_benchmark_instance(value.instance_id): raise HTTPException(400, "unknown benchmark instance")
    store.put_prediction(value)
    return value


@app.get("/api/results")
def results(instance_id: str | None = None, prediction_id: str | None = None):
    return store.list_evaluation_results(instance_id=instance_id, prediction_id=prediction_id)


@app.get("/api/results/{evaluation_id}")
def result(evaluation_id: str):
    value = store.get_evaluation_result(evaluation_id)
    if not value: raise HTTPException(404, "evaluation result not found")
    return value


@app.get("/api/validations")
def validations(instance_id: str | None = None):
    return store.list_instance_validations(instance_id=instance_id)


@app.get("/api/validations/latest/{instance_id}")
def latest_validation(instance_id: str):
    value = store.get_instance_validation(instance_id)
    if not value: raise HTTPException(404, "instance validation not found")
    return value


@app.get("/api/jobs")
def jobs(status: str | None = None, instance_id: str | None = None):
    return store.list_jobs(status=status, instance_id=instance_id)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    value = store.get_job(job_id)
    if not value: raise HTTPException(404, "benchmark job not found")
    return value


@app.get("/api/jobs/{job_id}/attempts")
def job_attempts(job_id: str):
    if not store.get_job(job_id): raise HTTPException(404, "benchmark job not found")
    return store.list_job_attempts(job_id)


@app.post("/api/jobs")
def create_job(request: BenchmarkJobCreate):
    if request.backend != "docker":
        raise HTTPException(400, "HTTP jobs require the isolated docker backend; use CLI for trusted local execution")
    if not store.get_benchmark_instance(request.instance_id): raise HTTPException(400, "unknown benchmark instance")
    if not store.get_evaluation_oracle(request.instance_id): raise HTTPException(400, "instance has no private evaluation oracle")
    if request.prediction_id:
        selected = store.get_prediction(request.prediction_id)
        if not selected or selected.instance_id != request.instance_id:
            raise HTTPException(400, "prediction does not belong to benchmark instance")
    value = BenchmarkJob(**request.model_dump())
    store.put_job(value)
    return value


@app.post("/api/generations")
def create_generation(request: GenerationJobCreate):
    if not store.get_benchmark_instance(request.instance_id):
        raise HTTPException(400, "unknown benchmark instance")
    if not store.get_evaluation_oracle(request.instance_id):
        raise HTTPException(400, "instance has no private evaluation oracle")
    model = AgentGenerator._opencode_model(request.model) if request.client == "opencode" else request.model
    value = BenchmarkJob(
        kind="generate_and_evaluate", instance_id=request.instance_id,
        backend=request.backend, workspace=request.workspace,
        max_attempts=request.max_attempts, client=request.client,
        model=model, workflow=request.workflow,
    )
    store.put_job(value)
    return value


@app.post("/api/comparisons")
def create_comparison(request: ComparisonRequest):
    """Enqueue every selected instance/model combination and return job ids."""
    # Validate the complete selection before writing anything, so a typo in one
    # test case cannot leave a partially-created comparison run in the queue.
    for instance_id in request.instance_ids:
        if not store.get_benchmark_instance(instance_id) or not store.get_evaluation_oracle(instance_id):
            raise HTTPException(400, f"instance or oracle not found: {instance_id}")
    batch_id = "cmp-" + uuid.uuid4().hex[:12]
    jobs = []
    for instance_id in request.instance_ids:
        for model in request.models:
            model = AgentGenerator._opencode_model(model) if request.client == "opencode" else model
            job = BenchmarkJob(kind="generate_and_evaluate", instance_id=instance_id,
                backend=request.backend, max_attempts=request.max_attempts,
                client=request.client, model=model, workflow=request.workflow, batch_id=batch_id)
            store.put_job(job); jobs.append(job)
    return {"batch_id": batch_id, "jobs": jobs, "count": len(jobs), "instance_ids": request.instance_ids,
            "models": sorted({job.model for job in jobs if job.model}),
            "status": "queued"}


@app.get("/api/comparisons/batches")
def comparison_batches():
    """List batch runs with live job progress for the dashboard."""
    batches = {}
    for job in store.list_jobs():
        if not job.batch_id:
            continue
        item = batches.setdefault(job.batch_id, {"batch_id": job.batch_id, "jobs": [], "created_at": job.created_at})
        item["jobs"].append(job)
    output = []
    for item in batches.values():
        jobs = item.pop("jobs")
        counts = {status: sum(job.status == status for job in jobs) for status in ("queued", "preparing", "generating", "evaluating", "completed", "failed", "cancelled")}
        output.append({**item, "job_count": len(jobs), "counts": counts,
            "completed": counts["completed"], "active": sum(counts[s] for s in ("queued", "preparing", "generating", "evaluating")),
            "failed": counts["failed"], "instance_ids": sorted({job.instance_id for job in jobs}),
            "models": sorted({job.model for job in jobs if job.model})})
    return sorted(output, key=lambda value: value["created_at"], reverse=True)


@app.post("/api/comparisons/report")
def comparison_report(request: ComparisonReportRequest):
    predictions = store.list_predictions()
    results = store.list_evaluation_results()
    prediction_ids = None
    run_metadata = None
    if request.batch_id:
        batch_jobs = [job for job in store.list_jobs() if job.batch_id == request.batch_id]
        run_metadata = {(job.instance_id, job.model): {"client": job.client, "workflow": job.workflow, "status": job.status, "error": job.error} for job in batch_jobs}
        prediction_ids = [job.prediction_id for job in batch_jobs if job.prediction_id]
        request.instance_ids = request.instance_ids or sorted({job.instance_id for job in batch_jobs})
        request.models = request.models or sorted({job.model for job in batch_jobs if job.model})
    return build_comparison_report(predictions, results, instance_ids=request.instance_ids, models=request.models,
        prediction_ids=prediction_ids, batch_id=request.batch_id, run_metadata=run_metadata)


@app.get("/api/comparisons/report")
def comparison_report_get(instance_ids: str | None = None, models: str | None = None, batch_id: str | None = None):
    prediction_ids = None
    run_metadata = None
    if batch_id:
        batch_jobs = [job for job in store.list_jobs() if job.batch_id == batch_id]
        run_metadata = {(job.instance_id, job.model): {"client": job.client, "workflow": job.workflow, "status": job.status, "error": job.error} for job in batch_jobs}
        prediction_ids = [job.prediction_id for job in batch_jobs if job.prediction_id]
        instance_ids = instance_ids or ",".join(sorted({job.instance_id for job in batch_jobs}))
        models = models or ",".join(sorted({job.model for job in batch_jobs if job.model}))
    return build_comparison_report(store.list_predictions(), store.list_evaluation_results(),
        instance_ids=_split_csv(instance_ids), models=_split_csv(models), prediction_ids=prediction_ids,
        batch_id=batch_id, run_metadata=run_metadata)


@app.get("/api/comparisons/report.csv")
def comparison_report_csv(instance_ids: str | None = None, models: str | None = None, batch_id: str | None = None):
    report = comparison_report_get(instance_ids=instance_ids, models=models, batch_id=batch_id)
    output = io.StringIO()
    writer = csv.writer(output)
    token_columns = ("avg_input_tokens", "avg_output_tokens", "total_input_tokens", "total_output_tokens", "total_tokens", "avg_total_tokens")
    columns = ("model", "runs", "resolved", "resolve_rate", "average_score", "functional_score", "code_quality_score", "documentation_score") + token_columns + ("avg_latency_ms",)
    writer.writerow(columns)
    for row in report["model_comparison"]:
        writer.writerow([row.get(key, "") for key in columns])
    return PlainTextResponse(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=sdd-model-comparison-report.csv"})


def _comparison_report_html(batch_id: str, report: dict) -> str:
    escape = lambda value: html.escape(str(value if value is not None else "-"))
    display_status = lambda value: {
        "queued": "排队中", "preparing": "准备中", "generating": "生成中", "evaluating": "评测中",
        "completed": "已完成", "failed": "失败", "cancelled": "已取消", "resolved": "已解决",
        "unresolved": "未解决", "regression": "发生回归", "target_tests_failed": "目标测试失败",
        "harness_error": "评测工具错误",
    }.get(value, value)
    model_rows = "".join(
        f"<tr><td><b>{escape(row['model'])}</b></td><td>{row['runs']}</td><td>{row['resolved']}</td>"
        f"<td>{row['resolve_rate'] * 100:.1f}%</td><td><b>{row['average_score']:.1f}</b></td>"
        f"<td>{row['functional_score']:.1f}</td><td>{row['code_quality_score']:.1f}</td>"
        f"<td>{row['documentation_score']:.1f}</td>"
        f"<td>{row.get('total_input_tokens', 0)} / {row.get('total_output_tokens', 0)} ({row.get('total_tokens', 0)} total)</td>"
        f"<td>{row['avg_latency_ms'] / 60000:.1f} 分钟</td></tr>"
        for row in sorted(
            report.get("model_comparison", []),
            key=lambda value: (-float(value.get("average_score", 0)), str(value.get("model", ""))),
        )
    )
    matrix_rows = "".join(
        f"<tr><td>{escape(row['instance_id'])}</td><td>{escape(row.get('client'))}</td>"
        f"<td>{escape(row.get('workflow'))}</td><td>{escape(row['model'])}</td>"
        f"<td><span class='status {escape(row.get('outcome') or row['status'])}'>{escape(display_status(row.get('outcome') or row['status']))}</span></td>"
        f"<td>{'-' if row.get('score') is None else format(row['score'], '.1f')}</td>"
        f"<td class='error'>{escape(row.get('error') or '')}</td></tr>"
        for row in report.get("instance_matrix", [])
    )
    detail_rows = "".join(
        f"<tr><td>{escape(row['instance_id'])}</td><td>{escape(row['model'])}</td><td>{escape(row['client'])}</td>"
        f"<td>{escape(row['workflow'])}</td><td>{escape(display_status(row['outcome']))}</td><td><b>{row['score']:.1f}</b></td>"
        f"<td>{row['functional_score']:.1f}</td><td>{row['code_quality_score']:.1f}</td><td>{row['documentation_score']:.1f}</td>"
        f"<td>{row['fail_to_pass']['passed']} / {row['fail_to_pass']['total']}</td>"
        f"<td>{row['pass_to_pass']['passed']} / {row['pass_to_pass']['total']}</td></tr>"
        for row in report.get("details", [])
    )
    completed = report.get("total_runs", 0)
    expected = report.get("expected_runs", 0)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>模型对比报告 · {escape(batch_id)}</title><style>
:root{{--navy:#10233f;--blue:#3973f3;--green:#16845d;--red:#c53e55;--line:#dfe7f1;--page:#f3f7fc}}*{{box-sizing:border-box}}body{{margin:0;background:var(--page);color:#1d2b42;font:14px/1.5 Inter,"Segoe UI",sans-serif}}header{{padding:30px 5%;color:white;background:linear-gradient(120deg,#0c1d36,#245492)}}main{{max-width:1450px;margin:auto;padding:24px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card,section{{padding:18px;border:1px solid var(--line);border-radius:12px;background:white;box-shadow:0 8px 24px #18375d10}}.card b{{display:block;font-size:26px}}section{{margin-top:18px;overflow:auto}}h1,h2{{margin-top:0}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #e8edf4;text-align:left;white-space:nowrap}}th{{color:#68778d;font-size:11px;text-transform:uppercase}}.status{{padding:3px 8px;border-radius:14px;background:#eaf1ff}}.resolved,.completed{{color:var(--green);background:#e7f7ef}}.failed,.unresolved,.regression,.target_tests_failed{{color:var(--red);background:#fff0f2}}.muted{{color:#718096}}@media(max-width:750px){{.cards{{grid-template-columns:1fr 1fr}}}}</style></head>
<body><header><h1>模型对比批次报表</h1><div>{escape(batch_id)} · 生成于浏览时的最新数据</div></header><main>
<div class="cards"><div class="card"><span class="muted">完成进度</span><b>{completed} / {expected}</b></div><div class="card"><span class="muted">测试用例</span><b>{len(report.get('instance_ids', []))}</b></div><div class="card"><span class="muted">模型</span><b>{len(report.get('models', []))}</b></div><div class="card"><span class="muted">整体解决数</span><b>{sum(row.get('resolved', 0) for row in report.get('model_comparison', []))}</b></div></div>
<section><h2>模型整体表现</h2><table><thead><tr><th>模型</th><th>运行数</th><th>解决数</th><th>解决率</th><th>综合分</th><th>功能</th><th>代码</th><th>文档</th><th>输入 / 输出 Token（总量）</th><th>平均耗时</th></tr></thead><tbody>{model_rows}</tbody></table></section>
<section><h2>测试用例 × 模型表现矩阵</h2><table><thead><tr><th>测试用例</th><th>编码工具</th><th>SDD 工具</th><th>模型</th><th>结果</th><th>综合分</th><th>错误信息</th></tr></thead><tbody>{matrix_rows}</tbody></table></section>
<section><h2>逐次评测明细</h2><table><thead><tr><th>测试用例</th><th>模型</th><th>编码工具</th><th>SDD 工具</th><th>结果</th><th>综合得分</th><th>功能得分</th><th>代码得分</th><th>文档得分</th><th>目标测试</th><th>回归测试</th></tr></thead><tbody>{detail_rows}</tbody></table></section>
</main></body></html>"""


@app.get("/api/comparisons/{batch_id}/report.html", response_class=HTMLResponse)
def comparison_batch_html_report(batch_id: str):
    jobs = [job for job in store.list_jobs() if job.batch_id == batch_id]
    if not jobs:
        raise HTTPException(404, "comparison batch not found")
    report = comparison_report_get(batch_id=batch_id)
    return HTMLResponse(_comparison_report_html(batch_id, report), headers={"Cache-Control": "no-store"})


@app.get("/api/comparisons/{batch_id}/report")
def comparison_batch_report(batch_id: str):
    return comparison_report_get(batch_id=batch_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    value = store.request_job_cancellation(job_id)
    if not value: raise HTTPException(404, "benchmark job not found")
    return value


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, replacement_model: str | None = None):
    current = store.get_job(job_id)
    allow_completed = False
    if current and current.kind == "generate_and_evaluate" and current.status == "completed" and current.result_id:
        result = store.get_evaluation_result(current.result_id)
        allow_completed = bool(result and not result.resolved)
    if replacement_model:
        replacement_model = replacement_model.strip()
        if not replacement_model:
            raise HTTPException(400, "replacement model must not be blank")
        if current and current.client == "opencode":
            replacement_model = AgentGenerator._opencode_model(replacement_model)
    value = store.retry_job(job_id, allow_completed=allow_completed, replacement_model=replacement_model)
    if not value: raise HTTPException(409, "job is missing or not retryable")
    return value
