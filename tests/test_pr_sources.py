import httpx
from fastapi.testclient import TestClient

from sdd_eval import api
from sdd_eval.pr_sources import PullRequestImport, PullRequestSourceService, size_matches
from sdd_eval.storage import Store


def response(request, status=200, json=None, text=None):
    return httpx.Response(status, json=json, text=text, request=request)


def test_size_ranges_have_unambiguous_boundaries():
    assert size_matches(499, "under_500")
    assert not size_matches(500, "under_500")
    assert size_matches(500, "500_to_1500")
    assert size_matches(1500, "500_to_1500")
    assert size_matches(1501, "over_1500")


def test_searches_github_by_fuzzy_name_and_language():
    def handler(request):
        assert request.url.path == "/search/repositories"
        assert request.url.params["q"] == "fast api language:Python"
        return response(request, json={"items": [{
            "full_name": "demo/fast-api", "name": "fast-api", "description": "Demo",
            "language": "Python", "stargazers_count": 42,
            "html_url": "https://github.com/demo/fast-api", "default_branch": "main",
        }]})

    service = PullRequestSourceService(httpx.Client(transport=httpx.MockTransport(handler)))
    [project] = service.search_repositories("github", "fast api", "Python")
    assert project.full_name == "demo/fast-api"
    assert project.stars == 42


def test_searches_gitcode_with_language_parameter():
    def handler(request):
        assert request.url.path == "/api/v5/search/repositories"
        assert request.url.params["q"] == "spring"
        assert request.url.params["language"] == "Java"
        return response(request, json=[{
            "full_name": "demo/spring", "name": "spring", "description": None,
            "language": "Java", "stars_count": 8, "default_branch": "master",
        }])

    service = PullRequestSourceService(httpx.Client(transport=httpx.MockTransport(handler)))
    [project] = service.search_repositories("gitcode", "spring", "Java")
    assert project.url == "https://gitcode.com/demo/spring"


def test_lists_only_merged_prs_in_selected_size_range():
    def handler(request):
        if request.url.path == "/search/issues":
            assert request.url.params["q"] == "repo:acme/app is:pr is:merged"
            return response(request, json={"items": [{"number": 3}, {"number": 4}]})
        number = int(request.url.path.rsplit("/", 1)[-1])
        lines = 500 if number == 3 else 1501
        return response(request, json={
            "number": number, "title": f"PR {number}", "body": "Details",
            "html_url": f"https://github.com/acme/app/pull/{number}", "state": "closed",
            "merged_at": "2026-09-01T00:00:00Z", "additions": lines - 1, "deletions": 1,
            "base": {"sha": "base"}, "head": {"sha": "head"},
        })

    service = PullRequestSourceService(httpx.Client(transport=httpx.MockTransport(handler)))
    prs = service.list_pull_requests("github", "acme/app", "500_to_1500")
    assert [item.number for item in prs] == [3]
    assert prs[0].changed_lines == 500


def test_import_endpoint_persists_instance_and_private_oracle(tmp_path, monkeypatch):
    diff = """diff --git a/app/core.py b/app/core.py
--- a/app/core.py
+++ b/app/core.py
@@ -1 +1,2 @@
 old behavior
+new_behavior = 'this uniquely identifies merged functionality'
"""

    def handler(request):
        if request.headers.get("accept") == "application/vnd.github.diff":
            return response(request, text=diff)
        return response(request, json={
            "number": 7, "title": "Add useful behavior", "body": "PR details",
            "html_url": "https://github.com/acme/app/pull/7", "state": "closed",
            "merged_at": "2026-09-01T00:00:00Z", "additions": 1, "deletions": 0,
            "base": {"sha": "base123"}, "head": {"sha": "head456"},
        })

    service = PullRequestSourceService(httpx.Client(transport=httpx.MockTransport(handler)))
    store = Store(str(tmp_path / "pr-import.db"))
    monkeypatch.setattr(api, "pr_source_service", service)
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)

    result = client.post("/api/pr-sources/import", json={
        "forge": "github", "repository": "acme/app", "number": 7, "language": "Python",
    })

    assert result.status_code == 200, result.text
    instance = store.get_benchmark_instance("github__acme__app-pr-7")
    oracle = store.get_evaluation_oracle(instance.instance_id)
    assert instance.base_commit == "base123"
    assert instance.source_pr_url.endswith("/pull/7")
    assert instance.reference_code_lines == 1
    assert oracle.gold_patch == diff
    assert oracle.reference_commit == "head456"
    assert oracle.fail_to_pass == [".sdd_eval_tests/pr_7_fail.py"]
    assert oracle.pass_to_pass == [".sdd_eval_tests/pr_7_pass.py"]
    assert "official PR marker" in oracle.test_patch

    duplicate = client.post("/api/pr-sources/import", json={
        "forge": "github", "repository": "acme/app", "number": 7,
    })
    assert duplicate.status_code == 409


def test_dashboard_exposes_pr_source_filters_and_actions():
    html = api.dashboard().body.decode()
    for value in ("GitHub", "GitCode", "sourceLanguage", "sourceProject", "under_500", "500_to_1500", "over_1500"):
        assert value in html
    assert "/api/pr-sources/repositories" in html
    assert "/api/pr-sources/pulls" in html
    assert "/api/pr-sources/import" in html
    assert '<script src="/dashboard.js"></script>' in html
    script = TestClient(api.app).get("/dashboard.js")
    assert script.status_code == 200
    assert "window.searchSourceRepositories" in script.text
    assert script.headers["content-type"].startswith("application/javascript")
    assert '<script src="/dashboard.js"></script>' in html
    assert "searchSourceRepositories" in api.dashboard_script().body.decode()


def test_gitcode_diff_is_rebuilt_from_documented_files_endpoint():
    def handler(request):
        assert request.url.path.endswith("/pulls/9/files")
        return response(request, json=[{
            "filename": "src/app.java", "status": "modified", "additions": "1", "deletions": "1",
            "patch": {"diff": "@@ -1 +1 @@\n-old implementation\n+new implementation with enough unique content"},
        }])

    service = PullRequestSourceService(httpx.Client(transport=httpx.MockTransport(handler)))
    patch = service._pull_diff("gitcode", "acme/app", 9)
    assert patch.startswith("diff --git a/src/app.java b/src/app.java")
    assert "--- a/src/app.java\n+++ b/src/app.java" in patch
    assert "+new implementation with enough unique content" in patch
