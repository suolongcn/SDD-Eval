import json
import urllib.parse
import urllib.request

from .models import ProjectSearchResult


def _get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sdd-eval"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def search_projects(provider: str, query: str, limit: int = 10):
    query = query.strip()
    if not query:
        return []
    encoded = urllib.parse.quote(query)
    try:
        if provider == "github":
            data = _get(f"https://api.github.com/search/repositories?q={encoded}&per_page={limit}")
            return [ProjectSearchResult(provider="github", name=x["full_name"], url=x["html_url"], description=x.get("description") or "", default_branch=x.get("default_branch") or "main", stars=x.get("stargazers_count", 0)) for x in data.get("items", [])]
        if provider == "gitee":
            data = _get(f"https://gitee.com/api/v5/search/repositories?q={encoded}&per_page={limit}")
            return [ProjectSearchResult(provider="gitee", name=x.get("full_name") or x.get("path", ""), url=x.get("html_url") or x.get("url", ""), description=x.get("description") or "", default_branch=x.get("default_branch") or "master", stars=x.get("stargazers_count", 0)) for x in data]
        if provider == "gitcode":
            # GitCode exposes a GitHub-compatible search API for public projects.
            data = _get(f"https://gitcode.com/api/v5/search/repositories?q={encoded}&per_page={limit}")
            return [ProjectSearchResult(provider="gitcode", name=x.get("full_name", ""), url=x.get("html_url", ""), description=x.get("description") or "", default_branch=x.get("default_branch") or "master", stars=x.get("stargazers_count", 0)) for x in data.get("items", data if isinstance(data, list) else [])]
    except Exception:
        return []
    return []


def classify_change(lines: int | None):
    if lines is None:
        return "unknown"
    if lines <= 500:
        return "small"
    if lines <= 1000:
        return "medium"
    return "large"


def pull_request_stats(provider: str, repo: str, number: int):
    try:
        if provider == "github":
            data = _get(f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100")
        else:
            data = _get(f"https://{provider}.com/api/v5/repos/{repo}/pulls/{number}/files?per_page=100")
        additions = sum(x.get("additions", 0) for x in data)
        deletions = sum(x.get("deletions", 0) for x in data)
        return {"files": len(data), "additions": additions, "deletions": deletions, "changed_lines": additions + deletions, "size_class": classify_change(additions + deletions)}
    except Exception as error:
        return {"error": str(error), "size_class": "unknown"}
