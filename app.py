"""Probe UI for an OpenAI-compatible vLLM server.

Configuration lives in the environment (or a .env file next to this script):
    VLLM_BASE_URL   e.g. http://localhost:8000/v1
    VLLM_MODEL      e.g. meta-llama/Llama-3.1-8B-Instruct
    VLLM_API_KEY    optional, blank for local servers
    FLASK_HOST      default 127.0.0.1
    FLASK_PORT      default 5050
    REQUEST_TIMEOUT default 120 (seconds)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

import bench

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

MAX_RUNS = 20


def load_env_file(path: Path) -> None:
    """Minimal .env loader so the app has no dependency beyond Flask + httpx."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def endpoint_from_env() -> bench.Endpoint:
    return bench.Endpoint(
        base_url=os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=os.environ.get("VLLM_API_KEY", ""),
        model=os.environ.get("VLLM_MODEL", ""),
        timeout=float(os.environ.get("REQUEST_TIMEOUT", "120")),
    )


def sse_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_float(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def list_models(endpoint: bench.Endpoint) -> list[str]:
    url = endpoint.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    try:
        with httpx.Client(timeout=min(endpoint.timeout, 4.0), trust_env=False) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        return [m["id"] for m in payload.get("data", []) if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []


def create_app(endpoint: bench.Endpoint | None = None) -> Flask:
    app = Flask(__name__)
    app.config["ENDPOINT"] = endpoint or endpoint_from_env()
    app.config["RUNS"] = {}
    app.config["RUNS_LOCK"] = threading.Lock()
    app.json.sort_keys = False

    def endpoint_now() -> bench.Endpoint:
        return app.config["ENDPOINT"]

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/config")
    def config():
        ep = endpoint_now()
        return jsonify(
            {
                "base_url": ep.base_url,
                "model": ep.model,
                "models": list_models(ep),
                "api_key_set": bool(ep.api_key),
                "timeout": ep.timeout,
            }
        )

    @app.post("/api/chat/stream")
    def chat_stream():
        body = request.get_json(silent=True) or {}
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return jsonify({"error": "messages must be a non-empty list"}), 400

        stream = bool(body.get("stream", True))
        max_tokens = clamp_int(body.get("max_tokens"), 1, 32768, 512)
        temperature = clamp_float(body.get("temperature"), 0.0, 2.0, 0.7)
        ep = endpoint_now()
        events: queue.Queue = queue.Queue()

        def work():
            try:
                result = bench.chat(
                    ep,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                    on_delta=lambda text: events.put(("delta", {"text": text})),
                )
                events.put(
                    (
                        "done",
                        {
                            "ok": result.ok,
                            "content": result.content,
                            "latency_ms": result.latency_ms,
                            "ttft_ms": result.ttft_ms,
                            "prompt_tokens": result.prompt_tokens,
                            "completion_tokens": result.completion_tokens,
                            "tokens_per_sec": result.tokens_per_sec,
                            "status": result.status,
                            "error": result.error,
                        },
                    )
                )
            except Exception as exc:
                events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
            finally:
                events.put(None)

        threading.Thread(target=work, daemon=True).start()

        def generate():
            while True:
                item = events.get()
                if item is None:
                    break
                name, data = item
                yield sse_event(name, data)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
        )

    @app.post("/api/loadtest")
    def start_loadtest():
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt is required"}), 400

        count = clamp_int(body.get("count"), 0, 1000, 10)
        concurrency = clamp_int(body.get("concurrency"), 0, 200, 4)
        max_tokens = clamp_int(body.get("max_tokens"), 1, 32768, 128)
        if count < 1 or concurrency < 1:
            return jsonify({"error": "count and concurrency must be at least 1"}), 400

        run = {
            "id": uuid.uuid4().hex[:12],
            "events": queue.Queue(),
            "cancel": threading.Event(),
            "summary": None,
        }
        ep = endpoint_now()
        stream = bool(body.get("stream", True))
        temperature = clamp_float(body.get("temperature"), 0.0, 2.0, 0.7)
        system_prompt = body.get("system_prompt") or ""

        def work():
            run["events"].put(
                ("start", {"count": count, "concurrency": concurrency, "stream": stream})
            )
            try:
                results, wall_ms = bench.run_load_test(
                    ep,
                    prompt=prompt,
                    count=count,
                    concurrency=concurrency,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                    system_prompt=system_prompt,
                    on_result=lambda r: run["events"].put(
                        ("result", {**r.public(), "index": r.index})
                    ),
                    on_delta=lambda index, text: run["events"].put(
                        ("delta", {"index": index, "text": text})
                    ),
                    cancel=run["cancel"],
                )
                summary = bench.summarize(results, wall_ms)
                run["summary"] = summary
                run["events"].put(("done", summary))
            except Exception as exc:
                run["events"].put(
                    ("error", {"message": f"{type(exc).__name__}: {exc}"})
                )
            finally:
                run["events"].put(None)

        with app.config["RUNS_LOCK"]:
            runs = app.config["RUNS"]
            while len(runs) >= MAX_RUNS:
                runs.pop(next(iter(runs)))
            runs[run["id"]] = run

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"id": run["id"]}), 202

    @app.get("/api/loadtest/<run_id>/stream")
    def loadtest_stream(run_id: str):
        with app.config["RUNS_LOCK"]:
            run = app.config["RUNS"].get(run_id)
        if run is None:
            return jsonify({"error": "unknown test id"}), 404

        def generate():
            while True:
                item = run["events"].get()
                if item is None:
                    break
                name, data = item
                yield sse_event(name, data)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers=SSE_HEADERS,
        )

    @app.post("/api/loadtest/<run_id>/stop")
    def stop_loadtest(run_id: str):
        with app.config["RUNS_LOCK"]:
            run = app.config["RUNS"].get(run_id)
        if run is None:
            return jsonify({"error": "unknown test id"}), 404
        run["cancel"].set()
        return jsonify({"stopping": True})

    return app


def main() -> None:
    load_env_file(Path(__file__).with_name(".env"))
    app = create_app()
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5050"))
    ep = app.config["ENDPOINT"]
    print(f"Probe UI  ->  http://{host}:{port}")
    print(f"Endpoint  ->  {ep.base_url}  (model: {ep.model or 'not set'})")
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
