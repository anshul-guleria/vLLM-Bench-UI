# Probe — vLLM bench UI

A local Flask UI for exercising an OpenAI-compatible vLLM endpoint:

- **Chat** — streamed multi-turn chat, timing every reply.
- **Single call** — one prompt, measured end to end; streaming optional.
- **Load test** — repeat one prompt under concurrency, with a live latency strip,
  live per-request outputs, percentiles, and per-request results.

## Run it

```powershell
python -m pip install -r requirements.txt
copy .env.example .env      # then edit .env
python app.py
```

Open <http://127.0.0.1:5050>.

`.env` is read at startup (values already set in the environment win):

| Variable | Meaning |
| --- | --- |
| `VLLM_BASE_URL` | OpenAI-compatible base, e.g. `http://localhost:8000/v1` |
| `VLLM_MODEL` | Model id exactly as vLLM reports it |
| `VLLM_API_KEY` | Optional bearer token; never sent to the browser |
| `FLASK_HOST` / `FLASK_PORT` | Where this UI listens (default `127.0.0.1:5050`) |
| `REQUEST_TIMEOUT` | Per-request timeout in seconds (default 120) |

## What the numbers mean

- **TTFT** — request sent to the first content token. Only shown when streaming.
- **Latency** — the full HTTP call, request sent to stream closed.
- **tok/s** — completion tokens ÷ generation time (total minus TTFT when streaming).
- **Requests / s** — successful requests ÷ total wall time of the load test.
- **Output tok / s** — successful completion tokens ÷ total wall time.
- Percentiles are linear-interpolated and calculated from **successful** requests only.

Times are measured server-side around the HTTP call with `time.perf_counter()`, so
they include this app's own overhead (usually sub-millisecond on localhost). Token
counts come from vLLM's `usage` block; if a server omits it, the engine estimates
about 4 characters per token and the UI still shows a number.

Load-test requests each open their own connection. That is deliberate for isolated
per-request timings, but if you are chasing peak throughput, results will be slightly
lower than a pooled client's.

## API used by the page

| Route | Purpose |
| --- | --- |
| `GET /api/config` | Endpoint, model, model list, whether a key is set |
| `POST /api/chat/stream` | SSE proxy: `delta`, `done`, `error` events |
| `POST /api/loadtest` | Start a run, returns `{id}` |
| `GET /api/loadtest/<id>/stream` | SSE: `start`, `delta`, `result`, `done`, `error` |
| `POST /api/loadtest/<id>/stop` | Cancel pending requests (in-flight ones finish) |

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

The tests run the engine and the Flask routes against a real local HTTP server that
speaks the OpenAI streaming and non-streaming formats — no mocked HTTP.
