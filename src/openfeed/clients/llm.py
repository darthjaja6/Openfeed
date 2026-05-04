"""LLM client.

openfeed currently supports **Gemini only** (transported via OpenRouter's
chat-completions API). The `GeminiRunner` class implements the `LLMRunner`
Protocol below — call sites are typed against the Protocol, so adding a
second backend (Anthropic, raw Google Generative AI SDK, local model)
would mean: new class implementing the Protocol + a small factory in
the `llm` section of the configured `openfeed.yaml` to pick which one.

We deliberately do NOT advertise "OpenAI-compatible" support — even
though OpenRouter speaks an OpenAI-shaped API, the schema-validation,
rate-limit, and finish-reason semantics are subtly different across
providers. Each backend is a first-class implementation.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

from urllib.request import Request, urlopen

from openfeed.utils.config_files import config_path, load_openfeed_config

logger = logging.getLogger("llm_trace")
logger.setLevel(logging.INFO)


def _scrub_for_trace(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace base64 image data URLs with size stubs. The raw bytes go to the
    LLM but logging them inflates each trace record by 100s of KB and they're
    unreadable in a jsonl viewer anyway."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_parts: list[Any] = []
        for part in content:
            url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
            if isinstance(url, str) and url.startswith("data:image"):
                kb = len(url) // 1024
                new_parts.append({
                    "type": part.get("type", "image_url"),
                    "image_url": {"url": f"<image:{kb}KB base64 stripped>"},
                })
            else:
                new_parts.append(part)
        out.append({**msg, "content": new_parts})
    return out


class LLMClientError(RuntimeError):
    pass


class LLMRunner(Protocol):
    """Stable contract every LLM backend implements. Callers depend on this,
    not on the concrete class, so a backend swap is type-safe.

    A runner returns the parsed JSON content of the LLM's response. Schema
    enforcement, retries, and request logging are the runner's job — call
    sites just pass `messages` (the chat history) and (optionally) the
    expected output schema."""

    def run_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "response",
    ) -> dict[str, Any]: ...


class GeminiRunner:
    """LLMRunner backed by Gemini-via-OpenRouter chat-completions API.

    Only backend currently shipped. Configuration in `openfeed.yaml`'s `llm`
    (`openrouter` section) + `OPENROUTER_API_KEY` env var."""

    def __init__(self, workdir: Path) -> None:
        del workdir
        raw = load_openfeed_config()
        cfg = raw.get("llm")
        if not isinstance(cfg, dict):
            raise LLMClientError(f"missing or invalid 'llm' section in {config_path()}")
        openrouter_cfg = cfg.get("openrouter") or {}
        api_key_env = str(openrouter_cfg.get("api_key_env", "OPENROUTER_API_KEY")).strip()
        self.api_key = os.environ.get(api_key_env, "").strip()
        self.model = str(openrouter_cfg.get("model", "")).strip()
        if not self.api_key or not self.model:
            raise LLMClientError(f"Missing {api_key_env} or llm.openrouter.model")

        self.api_base = str(openrouter_cfg.get("api_base", "https://openrouter.ai/api/v1")).rstrip("/")
        self.temperature = float(openrouter_cfg.get("temperature", 0) or 0)
        self.app_name = str(openrouter_cfg.get("app_name", "openfeed"))
        self.timeout_seconds = int(cfg.get("timeout_seconds", 240) or 240)

    def run_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "response",
    ) -> dict[str, Any]:
        """POST messages and return the parsed JSON content.

        When `schema` is given, response_format uses strict json_schema mode;
        otherwise it falls back to json_object mode.

        Retries once on transient upstream issues (empty content, content_filter).
        """
        last_exc: LLMClientError | None = None
        for attempt in range(2):
            try:
                return self._run_once(messages, schema=schema, schema_name=schema_name)
            except LLMClientError as exc:
                last_exc = exc
                # Only retry transient cases (empty content / content_filter).
                # JSON-decode errors after successful content aren't retried.
                if "empty content" not in str(exc) and "content_filter" not in str(exc):
                    raise
                time.sleep(2)
        raise last_exc  # type: ignore[misc]

    def _run_once(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None,
        schema_name: str,
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": self._response_format(schema, schema_name),
        }
        request = Request(
            url=f"{self.api_base}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Title": self.app_name,
            },
            method="POST",
        )
        start = time.time()
        record: dict[str, Any] = {"schema": schema_name, "messages": _scrub_for_trace(messages)}
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            choice = (envelope.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            record["content"] = content
            if not content:
                raise LLMClientError(f"empty content (finish_reason={choice.get('finish_reason')!r})")
            return json.loads(content)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, LLMClientError):
                raise
            raise LLMClientError(record["error"]) from exc
        finally:
            record["duration_ms"] = int((time.time() - start) * 1000)
            logger.info(json.dumps(record, ensure_ascii=False))

    @staticmethod
    def _response_format(schema: dict[str, Any] | None, schema_name: str) -> dict[str, Any]:
        if schema is None:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
