import os
import time
import json
from pathlib import Path

import httpx

from .models import TokenUsage


_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _post_with_retries(url: str, *, headers: dict, payload: dict, timeout: float):
    """POST to a model endpoint while tolerating transient proxy disconnects.

    Model gateways occasionally close an otherwise valid request or return a
    5xx response while they are overloaded.  A short bounded retry prevents a
    single gateway blip from turning into a run with no generated artifacts.
    Authentication and request-validation errors are returned immediately.
    """
    try:
        attempts = max(1, min(5, int(os.getenv("SDD_EVAL_PROVIDER_RETRIES", "3"))))
    except ValueError:
        attempts = 3

    last_error = None
    for attempt in range(attempts):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                response.close()
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.TransportError) as error:
            last_error = error
            if attempt + 1 >= attempts:
                raise
            time.sleep(2 ** attempt)
        except httpx.HTTPStatusError as error:
            # Retry only gateway/rate-limit responses.  A 4xx such as an
            # invalid key or model should be surfaced without delay.
            last_error = error
            if error.response.status_code not in _RETRYABLE_STATUS_CODES or attempt + 1 >= attempts:
                raise
            time.sleep(2 ** attempt)

    raise last_error or RuntimeError("model request failed")


class ModelProvider:
    name = "unknown"
    simulation = False

    def complete(self, prompt: str):
        raise NotImplementedError


class MockProvider(ModelProvider):
    """Pipeline dry-run provider; its output must never receive quality credit."""

    name = "mock"
    simulation = True

    def complete(self, prompt: str):
        text = "SIMULATION_ONLY: no model was called."
        return text, TokenUsage(input_tokens=0, output_tokens=0, estimated=True, provider=self.name, mode="simulation")


class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, base_url: str, api_key: str = "local", model: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = model

    def complete(self, prompt: str):
        started = time.perf_counter()
        response = _post_with_retries(
            self.base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload={"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
            timeout=300,
        )
        data = response.json()
        usage = data.get("usage") or {}
        content = data["choices"][0]["message"]["content"]
        estimated = not bool(usage)
        input_tokens = usage.get("prompt_tokens", 0) or max(1, len(prompt.split()))
        output_tokens = usage.get("completion_tokens", 0) or max(1, len(content.split()))
        return content, TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, estimated=estimated, latency_ms=int((time.perf_counter() - started) * 1000), provider=self.name, mode="model")


class CodexProvider(ModelProvider):
    """Uses the current Codex CLI configuration and auth file without copying credentials."""
    def __init__(self, requested_model: str | None = None):
        self.name = "codex"
        self.model = requested_model or "gpt-5.6-sol"
        self.base_url = "https://api.openai.com/v1"
        config = Path(os.getenv("USERPROFILE", "")) / ".codex" / "config.toml"
        if config.exists():
            raw = config.read_text(encoding="utf-8", errors="ignore")
            import re
            model = re.search(r"^model\s*=\s*\"([^\"]+)", raw, re.M)
            base = re.search(r"base_url\s*=\s*\"([^\"]+)", raw)
            if model and not requested_model: self.model = model.group(1)
            if base: self.base_url = base.group(1).rstrip("/")
        auth_file = Path(os.getenv("USERPROFILE", "")) / ".codex" / "auth.json"
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        if not self.api_key and auth_file.exists():
            try: self.api_key = json.loads(auth_file.read_text(encoding="utf-8")).get("OPENAI_API_KEY", "")
            except Exception: pass

    def complete(self, prompt: str):
        started = time.perf_counter()
        response = _post_with_retries(
            self.base_url + "/responses",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload={"model": self.model, "input": prompt},
            timeout=600,
        )
        data = response.json()
        content = data.get("output_text", "")
        if not content:
            content = "\n".join(item.get("text", "") for out in data.get("output", []) for item in out.get("content", []) if item.get("type") in ("output_text", "text"))
        usage = data.get("usage") or {}
        return content, TokenUsage(input_tokens=usage.get("input_tokens", len(prompt.split())), output_tokens=usage.get("output_tokens", len(content.split())), estimated=not bool(usage), latency_ms=int((time.perf_counter() - started) * 1000), provider=self.name + "/" + self.model, mode="model")


def provider_for(name: str):
    if not name or name == "mock":
        return MockProvider()
    if name in ("codex", "current-codex", "current_codex"):
        return CodexProvider()
    if name.startswith("codex:"):
        return CodexProvider(name.split(":", 1)[1])
    if name in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}:
        return CodexProvider(name)
    # A URL is accepted directly so Ollama/vLLM/OpenAI-compatible local servers work.
    return OpenAICompatibleProvider(name, api_key=os.getenv("SDD_EVAL_API_KEY", "local"), model=os.getenv("SDD_EVAL_MODEL", "default"))
