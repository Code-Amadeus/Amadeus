"""Small, explicit connection profiles for pure-local and Hybrid LLM endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import requests


LOCAL_BACKEND_TYPES = frozenset({"llama_server", "lmstudio", "ollama", "cli"})


def require_llama_message_capacity(
    url: str, payload: dict, *, timeout: float,
) -> dict[str, int]:
    """Count the loaded model's templated input before submitting generation.

    The server's per-slot n_ctx is authoritative, including for external servers.
    Never substitute the launch setting or truncate a role's instructions/history.
    """
    base = openai_base_url(url).removesuffix("/v1")
    try:
        response = requests.get(base + "/props", params={"model": payload["model"]}, timeout=timeout)
        response.raise_for_status()
        context = response.json()["default_generation_settings"]["n_ctx"]
        if type(context) is not int or context <= 0:
            raise ValueError("server did not report a positive per-slot n_ctx")
        # Use the same request, including output format/template options, that
        # generation will receive. Never count a reduced messages-only variant.
        response = requests.post(base + "/apply-template", json=payload, timeout=timeout)
        response.raise_for_status()
        prompt = response.json()["prompt"]
        if not isinstance(prompt, str):
            raise ValueError("server did not return a chat template")
        response = requests.post(base + "/tokenize", json={
            "model": payload["model"], "content": prompt, "add_special": True, "parse_special": True,
        }, timeout=timeout)
        response.raise_for_status()
        tokens = response.json()["tokens"]
        if not isinstance(tokens, list):
            raise ValueError("server did not return prompt tokens")
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Cannot verify llama-server context capacity. The server must expose "
            "/props, /apply-template and /tokenize for the selected model."
        ) from exc
    budget = {"input_tokens": len(tokens), "output_tokens": max(1, int(payload["max_tokens"])),
              "context_tokens": context}
    if budget["input_tokens"] + budget["output_tokens"] > context:
        raise RuntimeError(
            f"Local context is too small: {len(tokens)} input + "
            f"{budget['output_tokens']} output tokens exceeds the loaded {context}-token context. "
            "Increase the server context (LOCAL_LLM_CLI_CONTEXT for managed llama-server) "
            "or start a shorter conversation. No messages were truncated."
        )
    return budget


def openai_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def openai_chat_url(value: str) -> str:
    base = openai_base_url(value)
    return f"{base}/chat/completions"


def ollama_base_url(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if base.endswith("/api/chat"):
        return base[: -len("/api/chat")]
    if base.endswith("/api"):
        return base[: -len("/api")]
    if base.endswith("/v1"):
        return base[: -len("/v1")]
    return base


def ollama_chat_url(value: str) -> str:
    return f"{ollama_base_url(value)}/api/chat"


def local_chat_url(
    backend_type: str,
    *,
    llama_server_url: str,
    lmstudio_url: str,
    ollama_url: str,
) -> str:
    clean = str(backend_type or "").strip().lower()
    if clean == "ollama":
        return ollama_chat_url(ollama_url)
    if clean == "lmstudio":
        return openai_chat_url(lmstudio_url)
    if clean == "llama_server":
        return openai_chat_url(llama_server_url)
    if clean == "cli":
        return ""
    raise ValueError(f"unsupported local LLM backend: {backend_type!r}")


def _resolve_path(project_root: Path, value: str) -> Path | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    path = Path(clean)
    return path if path.is_absolute() else project_root / path


def should_manage_local_server(settings: Any) -> bool:
    return (
        str(settings.LLM_PROVIDER or "").strip().lower() == "local"
        and str(settings.LOCAL_LLM_TYPE or "").strip().lower() == "llama_server"
        and str(settings.LOCAL_LLM_LAUNCH_MODE or "").strip().lower() == "managed"
    )


def _probe_url(url: str, *, timeout_seconds: float = 0.7) -> tuple[bool, str]:
    try:
        response = httpx.get(url, timeout=max(0.1, float(timeout_seconds)))
        if response.status_code < 500:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, exc.__class__.__name__


def local_backend_status(settings: Any, *, project_root: Path) -> dict[str, Any]:
    backend_type = str(settings.LOCAL_LLM_TYPE or "llama_server").strip().lower()
    if backend_type not in LOCAL_BACKEND_TYPES:
        return {
            "configured": False,
            "available": False,
            "state": "unavailable",
            "detail": f"Unsupported local backend: {backend_type}",
        }

    if backend_type == "cli":
        executable = _resolve_path(project_root, settings.LOCAL_LLM_CLI_PATH)
        model = _resolve_path(project_root, settings.LOCAL_LLM_MODEL_PATH)
        missing = []
        if executable is None or not executable.is_file():
            missing.append("llama executable")
        if model is None or not model.is_file():
            missing.append("GGUF model")
        return {
            "configured": not missing,
            "available": False,
            "state": "unsupported",
            "detail": (
                "Persistent llama-cli does not isolate Cooperative Chat/Planner requests. "
                "Select llama_server and point the executable setting to llama-server; "
                "the existing GGUF can be reused with sufficient context."
            ),
        }

    if backend_type == "ollama":
        if not str(settings.LOCAL_LLM_OLLAMA_URL or "").strip():
            return {
                "configured": False,
                "available": False,
                "state": "not_configured",
                "detail": "Ollama URL is not configured",
            }
        endpoint = f"{ollama_base_url(settings.LOCAL_LLM_OLLAMA_URL)}/api/tags"
    elif backend_type == "lmstudio":
        if not str(settings.LOCAL_LLM_LM_STUDIO_URL or "").strip():
            return {
                "configured": False,
                "available": False,
                "state": "not_configured",
                "detail": "LM Studio URL is not configured",
            }
        endpoint = f"{openai_base_url(settings.LOCAL_LLM_LM_STUDIO_URL)}/models"
    else:
        if not str(settings.LOCAL_LLM_URL or "").strip():
            return {
                "configured": False,
                "available": False,
                "state": "not_configured",
                "detail": "llama.cpp server URL is not configured",
            }
        endpoint = f"{openai_base_url(settings.LOCAL_LLM_URL).removesuffix('/v1')}/health"

    reachable, observed = _probe_url(endpoint)
    launch_mode = str(settings.LOCAL_LLM_LAUNCH_MODE or "external").strip().lower()
    if backend_type == "llama_server" and launch_mode == "managed" and not reachable:
        executable = _resolve_path(project_root, settings.LOCAL_LLM_CLI_PATH)
        model = _resolve_path(project_root, settings.LOCAL_LLM_MODEL_PATH)
        missing = []
        if executable is None or not executable.is_file():
            missing.append("llama-server executable")
        if model is None or not model.is_file():
            missing.append("GGUF model")
        if missing:
            return {
                "configured": False,
                "available": False,
                "state": "not_configured",
                "detail": f"Managed launch is missing {', '.join(missing)}",
            }

    return {
        "configured": True,
        "available": reachable,
        "state": "available" if reachable else "unavailable",
        "detail": f"{backend_type} endpoint {observed}: {endpoint}",
    }


def hybrid_local_status(settings: Any) -> dict[str, Any]:
    configured = bool(
        str(settings.HYBRID_LOCAL_LLM_URL or "").strip()
        and str(settings.HYBRID_LOCAL_LLM_MODEL or "").strip()
    )
    if not configured:
        return {
            "configured": False,
            "available": False,
            "state": "not_configured",
            "detail": "Hybrid local-head endpoint and model are required",
        }
    endpoint = f"{openai_base_url(settings.HYBRID_LOCAL_LLM_URL)}/models"
    reachable, observed = _probe_url(endpoint)
    return {
        "configured": True,
        "available": reachable,
        "state": "available" if reachable else "unavailable",
        "detail": f"Hybrid local head {observed}: {endpoint}",
    }
