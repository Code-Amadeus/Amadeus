from __future__ import annotations

import argparse
import json
import sys
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    AWS_BEDROCK_AUTH_MODE,
    AWS_BEDROCK_BEARER_TOKEN,
    AWS_BEDROCK_ENDPOINT,
    AWS_BEDROCK_INFERENCE_PROFILE_ID,
    AWS_BEDROCK_MODEL_ID,
    AWS_BEDROCK_REGION,
    AWS_BEDROCK_USE_INFERENCE_PROFILE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
)
from llm.hybrid_stream import _SENTENCE_ENDINGS, _SENTINEL, _extract_token
from llm.prompts import get_system_prompt


@dataclass
class Sample:
    provider: str
    run: int
    ok: bool
    raw_ttft_ms: float | None
    first_sentence_ms: float | None
    tail_after_skip_ms: float | None
    done_ms: float | None
    chars: int
    error: str = ""


def _model_id() -> str:
    if AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
        return AWS_BEDROCK_INFERENCE_PROFILE_ID
    return AWS_BEDROCK_MODEL_ID


def _messages(prompt: str, provider: str) -> list[dict[str, str]]:
    kind = "bedrock" if provider == "bedrock" else "base"
    return [
        {"role": "system", "content": get_system_prompt(kind)},
        {"role": "user", "content": prompt},
    ]


def _measure_stream(provider: str, run: int, chunks: Iterable[str]) -> Sample:
    start = time.perf_counter()
    raw_ttft: float | None = None
    first_sentence: float | None = None
    tail_after_skip: float | None = None
    first_buf = ""
    total = ""

    for token in chunks:
        if not token:
            continue
        now = time.perf_counter()
        total += token
        if raw_ttft is None and token.strip():
            raw_ttft = (now - start) * 1000.0

        if first_sentence is None:
            first_buf += token
            for i, ch in enumerate(first_buf):
                if ch in _SENTENCE_ENDINGS:
                    first_sentence = (now - start) * 1000.0
                    remainder = first_buf[i + 1 :]
                    if remainder.strip():
                        tail_after_skip = (now - start) * 1000.0
                    break
        elif tail_after_skip is None and token.strip():
            tail_after_skip = (now - start) * 1000.0

    done = (time.perf_counter() - start) * 1000.0
    return Sample(
        provider=provider,
        run=run,
        ok=bool(total.strip()),
        raw_ttft_ms=raw_ttft,
        first_sentence_ms=first_sentence,
        tail_after_skip_ms=tail_after_skip,
        done_ms=done,
        chars=len(total),
    )


def _deepseek_chunks(prompt: str, max_tokens: int) -> Iterable[str]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    from openai import OpenAI
    import httpx

    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        http_client=httpx.Client(
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30.0),
            timeout=httpx.Timeout(30.0),
            http2=False,
        ),
    )
    stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=_messages(prompt, "deepseek"),
        temperature=0.7,
        max_tokens=max_tokens,
        stream=True,
        timeout=20,
        extra_body={"thinking": {"type": "disabled"}},
    )
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        token = getattr(delta, "content", None) or getattr(delta, "text", None) or ""
        if token:
            yield token


def _bedrock_boto3_chunks(prompt: str, max_tokens: int) -> Iterable[str]:
    if AWS_BEDROCK_AUTH_MODE == "bearer":
        raise ImportError("BEDROCK_AUTH_MODE=bearer")
    import boto3

    payload = {
        "model": _model_id(),
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "messages": _messages(prompt, "bedrock"),
        "stream": True,
    }
    client = boto3.client("bedrock-runtime", region_name=AWS_BEDROCK_REGION)
    stream = client.invoke_model_with_response_stream(
        modelId=_model_id(),
        body=json.dumps(payload),
    ).get("body")
    if not stream:
        return
    for event in stream:
        if "chunk" not in event:
            continue
        try:
            data = json.loads(event["chunk"]["bytes"].decode("utf-8"))
        except Exception:
            continue
        token = _extract_token(data)
        if token is _SENTINEL:
            return
        if token:
            yield token


def _bedrock_http_chunks(prompt: str, max_tokens: int) -> Iterable[str]:
    if not AWS_BEDROCK_BEARER_TOKEN:
        raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK is not configured")
    payload = {
        "model": _model_id(),
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "messages": _messages(prompt, "bedrock"),
        "stream": True,
    }
    url = f"{AWS_BEDROCK_ENDPOINT}/model/{_model_id()}/invoke-with-response-stream"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AWS_BEDROCK_BEARER_TOKEN}",
    }
    with requests.post(url, headers=headers, json=payload, timeout=60, stream=True) as response:
        response.raise_for_status()
        buf = b""
        for raw_chunk in response.iter_content(chunk_size=8192):
            if not raw_chunk:
                continue
            buf += raw_chunk
            while len(buf) >= 12:
                total_len = int.from_bytes(buf[:4], "big")
                hdr_len = int.from_bytes(buf[4:8], "big")
                if total_len < 16 or hdr_len > total_len - 16:
                    buf = b""
                    break
                if len(buf) < total_len:
                    break
                evt = buf[:total_len]
                buf = buf[total_len:]
                payload_bytes = evt[12 + hdr_len : total_len - 4]
                try:
                    payload_text = payload_bytes.decode("utf-8", errors="ignore")
                    json_start = payload_text.find("{")
                    if json_start > 0:
                        payload_text = payload_text[json_start:]
                    data = json.loads(payload_text)
                    if "bytes" in data:
                        import base64

                        data = json.loads(base64.b64decode(data["bytes"]))
                except Exception:
                    continue
                token = _extract_token(data)
                if token is _SENTINEL:
                    return
                if token:
                    yield token


def _bedrock_chunks(prompt: str, max_tokens: int) -> Iterable[str]:
    try:
        yield from _bedrock_boto3_chunks(prompt, max_tokens)
        return
    except ImportError:
        pass
    except Exception as exc:
        if AWS_BEDROCK_AUTH_MODE == "boto3":
            raise
        print(f"[bedrock] boto3 failed, trying HTTP fallback: {exc}")
    yield from _bedrock_http_chunks(prompt, max_tokens)


def _run_provider(provider: str, run: int, prompt: str, max_tokens: int) -> Sample:
    try:
        if provider == "deepseek":
            return _measure_stream(provider, run, _deepseek_chunks(prompt, max_tokens))
        if provider == "bedrock":
            return _measure_stream(provider, run, _bedrock_chunks(prompt, max_tokens))
        raise ValueError(f"unknown provider: {provider}")
    except Exception as exc:
        return Sample(
            provider=provider,
            run=run,
            ok=False,
            raw_ttft_ms=None,
            first_sentence_ms=None,
            tail_after_skip_ms=None,
            done_ms=None,
            chars=0,
            error=str(exc),
        )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _summary(samples: list[Sample], field: str) -> str:
    values = [getattr(s, field) for s in samples if s.ok and getattr(s, field) is not None]
    if not values:
        return "-"
    values = sorted(float(v) for v in values)
    p50 = statistics.median(values)
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    return f"p50={p50:.0f}ms p90={p90:.0f}ms"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare DeepSeek Flash and Bedrock stream latency.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument(
        "--prompt",
        default=(
            "The user asks: please briefly explain whether switching the continuation "
            "provider would hurt real-time voice flow. Reply in Japanese as Kurisu, "
            "concise but with enough content for two short sentences."
        ),
    )
    args = parser.parse_args()

    providers = ["bedrock", "deepseek"]
    measured: list[Sample] = []
    for provider in providers:
        for i in range(args.warmup):
            sample = _run_provider(provider, -(i + 1), args.prompt, args.max_tokens)
            print(
                f"warmup {provider}: ok={sample.ok} raw={_fmt(sample.raw_ttft_ms)}ms "
                f"tail={_fmt(sample.tail_after_skip_ms)}ms done={_fmt(sample.done_ms)}ms"
            )
        for run in range(1, args.runs + 1):
            sample = _run_provider(provider, run, args.prompt, args.max_tokens)
            measured.append(sample)
            print(
                f"{provider} run={run} ok={sample.ok} raw_ttft={_fmt(sample.raw_ttft_ms)}ms "
                f"first_sentence={_fmt(sample.first_sentence_ms)}ms "
                f"tail_after_skip={_fmt(sample.tail_after_skip_ms)}ms "
                f"done={_fmt(sample.done_ms)}ms chars={sample.chars}"
                + (f" error={sample.error}" if sample.error else "")
            )

    print("\nsummary:")
    for provider in providers:
        samples = [s for s in measured if s.provider == provider]
        print(f"{provider}:")
        print(f"  raw_ttft:        {_summary(samples, 'raw_ttft_ms')}")
        print(f"  first_sentence:  {_summary(samples, 'first_sentence_ms')}")
        print(f"  tail_after_skip: {_summary(samples, 'tail_after_skip_ms')}")
        print(f"  done:            {_summary(samples, 'done_ms')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
