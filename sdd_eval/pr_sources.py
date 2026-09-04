"""Discover merged pull requests and turn them into executable benchmarks."""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from .models import BenchmarkInstance, EnvironmentSpec, EvaluationOracle, RequirementIR


Forge = Literal["github", "gitcode"]
SizeRange = Literal["under_500", "500_to_1500", "over_1500", "all"]


class RepositoryCandidate(BaseModel):
    forge: Forge
    full_name: str
    name: str
    description: str = ""
    language: str = ""
    stars: int = 0
    url: str
    default_branch: str = ""


class PullRequestCandidate(BaseModel):
    forge: Forge
    repository: str
    number: int
    title: str
    body: str = ""
    url: str
    state: str = ""
    merged_at: str | None = None
    base_commit: str = ""
    head_commit: str = ""
    additions: int = 0
    deletions: int = 0
    changed_lines: int = 0


class PullRequestImport(BaseModel):
    forge: Forge
    repository: str = Field(pattern=r"^[^/\s]+(?:/[^/\s]+)+$")
    number: int = Field(gt=0)
    language: str = ""


def size_matches(lines: int, size: SizeRange) -> bool:
    return size == "all" or (size == "under_500" and lines < 500) or (
        size == "500_to_1500" and 500 <= lines <= 1500
    ) or (size == "over_1500" and lines > 1500)


def _added_file_patch(path: str, content: str) -> str:
    lines = content.splitlines()
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "\n".join("+" + line for line in lines) + "\n"
    )


def _smoke_oracle(number: int, patch: str) -> tuple[str, list[str], list[str]]:
    """Build a language-independent base-vs-PR Oracle from an added source line."""
    path = ""
    choices: list[tuple[int, str, str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            path = line[len("diff --git a/"):].split(" b/", 1)[0]
        elif path and line.startswith("+") and not line.startswith("+++"):
            marker = line[1:]
            normalized = path.lower()
            if len(marker.strip()) >= 16 and not marker.strip().startswith(("#", "//", "*")):
                penalty = 10_000 if any(x in normalized for x in ("test", "docs/", "readme", ".github", "lock")) else 0
                choices.append((penalty - min(len(marker.strip()), 500), path, marker))
    if not choices:
        raise ValueError("PR diff does not contain a stable added source line for an executable Oracle")
    _, target, marker = min(choices)
    slug = f"pr_{number}"
    fail_path, pass_path = f".sdd_eval_tests/{slug}_fail.py", f".sdd_eval_tests/{slug}_pass.py"
    fail = "\n".join((
        "from pathlib import Path", f"target = Path({target!r})", f"marker = {marker!r}",
        "assert target.is_file(), f'expected PR file is missing: {target}'",
        "assert marker in target.read_text(encoding='utf-8', errors='replace'), f'official PR marker is missing from {target}'",
    )) + "\n"
    passed = "from pathlib import Path\nassert Path('.git').is_dir(), 'checkout is not a Git repository'\n"
    return _added_file_patch(fail_path, fail) + _added_file_patch(pass_path, passed), [fail_path], [pass_path]


class PullRequestSourceService:
    BASES = {"github": "https://api.github.com", "gitcode": "https://api.gitcode.com/api/v5"}

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)

    def _headers(self, forge: Forge, *, diff: bool = False) -> dict[str, str]:
        headers = {"User-Agent": "SDD-Eval/2.0", "Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN" if forge == "github" else "GITCODE_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if forge == "github":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if diff:
                headers["Accept"] = "application/vnd.github.diff"
        return headers

    def _get(self, forge: Forge, path: str, *, params: dict | None = None, diff: bool = False):
        response = self.client.get(self.BASES[forge] + path, params=params, headers=self._headers(forge, diff=diff))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = "access token is required or invalid" if response.status_code in (401, 403) else response.text[:300]
            raise RuntimeError(f"{forge} API request failed ({response.status_code}): {message}") from exc
        return response

    def search_repositories(self, forge: Forge, name: str, language: str = "", limit: int = 20) -> list[RepositoryCandidate]:
        limit = max(1, min(limit, 50 if forge == "gitcode" else 100))
        if forge == "github":
            query = name.strip() or "stars:>100"
            if language.strip():
                query += f" language:{language.strip()}"
            payload = self._get(forge, "/search/repositories", params={"q": query, "per_page": limit, "sort": "stars"}).json().get("items", [])
        else:
            if not name.strip():
                raise ValueError("GitCode repository search requires a project name")
            params = {"q": name.strip(), "per_page": min(limit, 20), "sort": "stars_count"}
            if language.strip(): params["language"] = language.strip()
            payload = self._get(forge, "/search/repositories", params=params).json()
        output = []
        for item in payload:
            full_name = item.get("full_name") or item.get("path_with_namespace")
            if not full_name: continue
            output.append(RepositoryCandidate(
                forge=forge, full_name=full_name, name=item.get("name") or full_name.rsplit("/", 1)[-1],
                description=item.get("description") or "", language=item.get("language") or "",
                stars=item.get("stargazers_count") or item.get("stars_count") or 0,
                url=item.get("html_url") or item.get("web_url") or f"https://{forge}.com/{full_name}",
                default_branch=item.get("default_branch") or "",
            ))
        return output

    def list_pull_requests(self, forge: Forge, repository: str, size: SizeRange = "all", limit: int = 30) -> list[PullRequestCandidate]:
        repo = quote(repository, safe="/")
        if forge == "github":
            payload = self._get(forge, "/search/issues", params={
                "q": f"repo:{repository} is:pr is:merged", "per_page": min(max(limit, 1), 100), "sort": "updated",
            }).json()
            rows = payload.get("items", [])
        else:
            params = {"state": "merged", "per_page": min(max(limit, 1), 100), "sort": "updated", "direction": "desc"}
            rows = self._get(forge, f"/repos/{repo}/pulls", params=params).json()
        output = []
        for row in rows:
            number = int(row.get("number") or row.get("iid"))
            candidate = self._pull_request(forge, repository, number)
            if candidate.merged_at and size_matches(candidate.changed_lines, size):
                output.append(candidate)
        return output

    def _pull_request(self, forge: Forge, repository: str, number: int) -> PullRequestCandidate:
        repo = quote(repository, safe="/")
        detail = self._get(forge, f"/repos/{repo}/pulls/{number}").json()
        merged_at = detail.get("merged_at") or detail.get("merged_date")
        additions, deletions = int(detail.get("additions") or 0), int(detail.get("deletions") or 0)
        base, head = detail.get("base") or {}, detail.get("head") or {}
        return PullRequestCandidate(
            forge=forge, repository=repository, number=number, title=detail.get("title") or "",
            body=detail.get("body") or detail.get("description") or "", url=detail.get("html_url") or detail.get("web_url") or "",
            state=detail.get("state") or "", merged_at=merged_at,
            base_commit=base.get("sha") or detail.get("base_sha") or "",
            head_commit=head.get("sha") or detail.get("head_sha") or detail.get("merge_commit_sha") or "",
            additions=additions, deletions=deletions, changed_lines=additions + deletions,
        )

    def _pull_diff(self, forge: Forge, repository: str, number: int) -> str:
        repo = quote(repository, safe="/")
        if forge == "github":
            return self._get(forge, f"/repos/{repo}/pulls/{number}", diff=True).text
        # GitCode exposes per-file hunks rather than a documented .diff media
        # type. Rebuild a standard unified diff that git apply can consume.
        files = self._get(forge, f"/repos/{repo}/pulls/{number}/files").json()
        chunks = []
        for item in files:
            patch = item.get("patch") or item.get("diff") or ""
            if isinstance(patch, dict):
                patch = patch.get("diff") or ""
            old_path = item.get("old_path") or item.get("filename") or ""
            new_path = item.get("new_path") or item.get("filename") or old_path
            if not patch or not (old_path or new_path):
                continue
            old_label = "/dev/null" if item.get("new_file") or item.get("status") == "added" else f"a/{old_path}"
            new_label = "/dev/null" if item.get("deleted_file") or item.get("status") == "removed" else f"b/{new_path}"
            chunks.append(f"diff --git a/{old_path or new_path} b/{new_path or old_path}\n--- {old_label}\n+++ {new_label}\n{patch.rstrip()}\n")
        return "".join(chunks)

    def import_pull_request(self, request: PullRequestImport) -> tuple[BenchmarkInstance, EvaluationOracle]:
        pr = self._pull_request(request.forge, request.repository, request.number)
        if not pr.merged_at:
            raise ValueError("merged pull request was not found")
        patch = self._pull_diff(request.forge, request.repository, request.number)
        if not patch.lstrip().startswith("diff --git"):
            raise ValueError("forge did not return a unified PR diff")
        test_patch, f2p, p2p = _smoke_oracle(request.number, patch)
        slug = re.sub(r"[^a-zA-Z0-9_.-]", "__", request.repository)
        instance_id = f"{request.forge}__{slug}-pr-{request.number}"
        language = request.language or "unknown"
        instance = BenchmarkInstance(
            instance_id=instance_id, dataset_id=f"{request.forge}-merged-prs", dataset_version="live-v1", split="dev",
            repo=f"https://{request.forge}.com/{request.repository}.git", base_commit=pr.base_commit,
            problem_statement=pr.title + (("\n\n" + pr.body) if pr.body else ""), language=language,
            environment=EnvironmentSpec(test_command=["python", "{tests}"]),
            requirements=[RequirementIR(id=f"PR-{request.number}", description=pr.title,
                acceptance_criteria=["The merged pull request behavior is preserved."], source_refs=[pr.url])],
            constraints=["Preserve behavior outside the pull request scope", "Do not modify hidden Oracle tests"],
            source_pr_url=pr.url, reference_code_lines=pr.changed_lines,
        )
        oracle = EvaluationOracle(instance_id=instance_id, gold_patch=patch, test_patch=test_patch,
            fail_to_pass=f2p, pass_to_pass=p2p, reference_commit=pr.head_commit,
            expected_results={"source": "merged-pr", "forge": request.forge})
        return instance, oracle
