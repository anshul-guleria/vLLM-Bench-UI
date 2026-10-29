"""Core engine: talk to an OpenAI-compatible endpoint and measure it.

Everything timing-related is measured on this side of the wire, with
time.perf_counter(), so the numbers reflect what a client actually waits for.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

import httpx

# vLLM reports usage on the final chunk only when asked to.
STREAM_OPTIONS = {"include_usage": True}


@dataclass
class Endpoint:
    base_url: str
    api_key: str = ""
    model: str = ""
    timeout: float = 120.0


@dataclass
class Result:
    ok: bool
    latency_ms: float
    ttft_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_sec: float | None = None
    content: str = ""
    error: str | None = None
    status: int | None = None
    index: int = -1
    started_at: float = 0.0
    finished_at: float = 0.0

    def public(self) -> dict:
        data = asdict(self)
        data.pop("started_at")
        data.pop("finished_at")
        data["start_ms"] = self.started_at
        data["end_ms"] = self.finished_at
        return data


def percentile(values: Iterable[float], p: float) -> float:
    """Linear-interpolated percentile. p in 0..100. Empty -> 0.0."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def estimate_tokens(text: str) -> int:
    """Rough fallback when the server does not report usage: ~4 chars/token."""
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _headers(endpoint: Endpoint) -> dict:
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    return headers


def _payload(
    endpoint: Endpoint,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    stream: bool,
) -> dict:
    payload = {
        "model": endpoint.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if stream:
        payload["stream"] = True
        payload["stream_options"] = dict(STREAM_OPTIONS)
    return payload


def _error_from_response(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = body.get("error") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("message") or json.dumps(detail)
    return str(detail) if detail else f"HTTP {response.status_code}"


def chat(
    endpoint: Endpoint,
    messages: list[dict],
    max_tokens: int = 128,
    temperature: float = 0.7,
    stream: bool = False,
    on_delta: Callable[[str], None] | None = None,
) -> Result:
    url = endpoint.base_url.rstrip("/") + "/chat/completions"
    started = time.perf_counter()

    if not stream:
        try:
            with httpx.Client(timeout=endpoint.timeout, trust_env=False) as client:
                response = client.post(
                    url,
                    headers=_headers(endpoint),
                    json=_payload(endpoint, messages, max_tokens, temperature, False),
                )
        except Exception as exc:  # network, timeout, DNS
            return Result(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started,
                finished_at=time.perf_counter(),
            )

        finished = time.perf_counter()
        latency_ms = (finished - started) * 1000
        if response.status_code != 200:
            return Result(
                ok=False,
                latency_ms=latency_ms,
                status=response.status_code,
                error=_error_from_response(response),
                started_at=started,
                finished_at=finished,
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"].get("content") or ""
            usage = body.get("usage") or {}
            completion = int(usage.get("completion_tokens") or estimate_tokens(content))
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
        except (ValueError, KeyError, IndexError) as exc:
            return Result(
                ok=False,
                latency_ms=latency_ms,
                status=response.status_code,
                error=f"Unreadable response: {exc}",
                started_at=started,
                finished_at=finished,
            )

        return Result(
            ok=True,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion,
            tokens_per_sec=completion / latency_ms * 1000 if latency_ms else None,
            content=content,
            status=response.status_code,
            started_at=started,
            finished_at=finished,
        )

    # Streaming: time the first visible token separately from the whole response.
    pieces: list[str] = []
    completion = 0
    prompt_tokens = 0
    ttft_ms: float | None = None
    try:
        with httpx.Client(timeout=endpoint.timeout, trust_env=False) as client:
            with client.stream(
                "POST",
                url,
                headers=_headers(endpoint),
                json=_payload(endpoint, messages, max_tokens, temperature, True),
            ) as response:
                if response.status_code != 200:
                    response.read()
                    finished = time.perf_counter()
                    return Result(
                        ok=False,
                        latency_ms=(finished - started) * 1000,
                        status=response.status_code,
                        error=_error_from_response(response),
                        started_at=started,
                        finished_at=finished,
                    )

                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    usage = chunk.get("usage") or {}
                    if usage:
                        completion = int(usage.get("completion_tokens") or completion)
                        prompt_tokens = int(usage.get("prompt_tokens") or prompt_tokens)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            if ttft_ms is None:
                                ttft_ms = (time.perf_counter() - started) * 1000
                            pieces.append(text)
                            if on_delta is not None:
                                on_delta(text)
    except Exception as exc:
        finished = time.perf_counter()
        return Result(
            ok=False,
            latency_ms=(finished - started) * 1000,
            ttft_ms=ttft_ms,
            content="".join(pieces),
            error=f"{type(exc).__name__}: {exc}",
            started_at=started,
            finished_at=finished,
        )

    finished = time.perf_counter()
    latency_ms = (finished - started) * 1000
    content = "".join(pieces)
    if not completion:
        completion = estimate_tokens(content)
    generation_ms = latency_ms - (ttft_ms or 0.0)
    return Result(
        ok=True,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion,
        tokens_per_sec=completion / generation_ms * 1000 if generation_ms > 0 else None,
        content=content,
        status=200,
        started_at=started,
        finished_at=finished,
    )


def messages_for(prompt: str, system_prompt: str = "") -> list[dict]:
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def run_load_test(
    endpoint: Endpoint,
    prompt: str,
    count: int = 10,
    concurrency: int = 4,
    max_tokens: int = 128,
    temperature: float = 0.7,
    stream: bool = True,
    system_prompt: str = "",
    on_result: Callable[[Result], None] | None = None,
    on_delta: Callable[[int, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[Result], float]:
    """Fire `count` requests with `concurrency` in flight. Returns (results, wall_ms)."""
    messages = messages_for(prompt, system_prompt)
    t0 = time.perf_counter()

    def task(index: int) -> Result | None:
        if cancel is not None and cancel.is_set():
            return None
        request_started = time.perf_counter()
        result = chat(
            endpoint,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            on_delta=(lambda text: on_delta(index, text)) if on_delta else None,
        )
        request_finished = time.perf_counter()
        result.index = index
        result.started_at = max(0.0, (request_started - t0) * 1000)
        result.finished_at = max(0.0, (request_finished - t0) * 1000)
        return result

    results: list[Result] = []
    workers = max(1, min(concurrency, count))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, i) for i in range(count)]
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            results.append(result)
            if on_result is not None:
                on_result(result)

    wall_ms = (time.perf_counter() - t0) * 1000
    results.sort(key=lambda r: r.index)
    return results, wall_ms


def summarize(results: list[Result], wall_ms: float) -> dict:
    ok_results = [r for r in results if r.ok]
    latencies = [r.latency_ms for r in ok_results]
    ttfts = [r.ttft_ms for r in ok_results if r.ttft_ms is not None]
    output_tokens = sum(r.completion_tokens for r in ok_results)
    prompt_tokens = sum(r.prompt_tokens for r in ok_results)
    wall_s = wall_ms / 1000 if wall_ms else 0

    def stats(values: list[float]) -> dict | None:
        if not values:
            return None
        return {
            "p50": percentile(values, 50),
            "p90": percentile(values, 90),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
        }

    return {
        "total": len(results),
        "ok": len(ok_results),
        "failed": len(results) - len(ok_results),
        "error_rate": (len(results) - len(ok_results)) / len(results) if results else 0.0,
        "wall_ms": wall_ms,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "throughput_rps": len(ok_results) / wall_s if wall_s else 0.0,
        "output_tps": output_tokens / wall_s if wall_s else 0.0,
        "latency": stats(latencies),
        "ttft": stats(ttfts),
    }
