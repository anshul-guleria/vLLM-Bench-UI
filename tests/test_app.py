"""Tests for the Flask layer: SSE shaping, config leak safety, validation."""

import json

import pytest

from app import create_app
from bench import Endpoint


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        name, payload = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:].strip())
        if name:
            events.append((name, payload))
    return events


@pytest.fixture
def client(fake):
    app = create_app(
        Endpoint(base_url=fake.base_url, api_key="super-secret", model="fake-model", timeout=10)
    )
    app.config.update(TESTING=True)
    return app.test_client()


def test_config_exposes_model_and_never_the_key(client):
    response = client.get("/api/config")
    body = response.get_json()

    assert response.status_code == 200
    assert body["model"] == "fake-model"
    assert body["api_key_set"] is True
    assert "super-secret" not in response.get_data(as_text=True)
    assert "fake-model" in body["models"]


def test_streaming_chat_emits_deltas_then_metrics(client):
    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "max_tokens": 8,
            "temperature": 0.0,
        },
    )
    events = parse_sse(response.get_data(as_text=True))
    names = [name for name, _ in events]

    assert "delta" in names
    assert names[-1] == "done"
    done = events[-1][1]
    assert done["content"] == "Hello from the fake"
    assert done["ttft_ms"] is not None
    assert done["completion_tokens"] == 4


def test_non_streaming_chat_still_reports_a_single_result(client):
    response = client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "max_tokens": 8,
            "temperature": 0.0,
        },
    )
    events = parse_sse(response.get_data(as_text=True))

    assert [name for name, _ in events] == ["done"]
    assert events[0][1]["content"] == "Hello from the fake"


def test_load_test_streams_results_and_a_summary(client):
    start = client.post(
        "/api/loadtest",
        json={
            "prompt": "load",
            "count": 3,
            "concurrency": 2,
            "max_tokens": 8,
            "temperature": 0.0,
            "stream": True,
        },
    )
    run_id = start.get_json()["id"]
    response = client.get(f"/api/loadtest/{run_id}/stream")
    events = parse_sse(response.get_data(as_text=True))

    results = [payload for name, payload in events if name == "result"]
    done = [payload for name, payload in events if name == "done"]
    assert len(results) == 3
    assert all(r["ok"] for r in results)
    assert all(r["ttft_ms"] is not None for r in results)
    assert len(done) == 1
    assert done[0]["total"] == 3
    assert done[0]["latency"]["p50"] > 0


def test_load_test_rejects_nonsense_parameters(client):
    response = client.post(
        "/api/loadtest", json={"prompt": "x", "count": 0, "concurrency": 0}
    )
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_load_test_streams_output_deltas_tagged_per_request(client):
    start = client.post(
        "/api/loadtest",
        json={
            "prompt": "live outputs",
            "count": 2,
            "concurrency": 2,
            "max_tokens": 8,
            "temperature": 0.0,
            "stream": True,
        },
    )
    run_id = start.get_json()["id"]
    events = parse_sse(client.get(f"/api/loadtest/{run_id}/stream").get_data(as_text=True))

    deltas = [(name, payload) for name, payload in events if name == "delta"]
    results = {payload["index"]: payload for name, payload in events if name == "result"}

    assert deltas, "no streaming deltas reached the client"
    assert all(payload["index"] in results for _, payload in deltas)
    for index, result in results.items():
        streamed = "".join(p["text"] for _, p in deltas if p["index"] == index)
        assert streamed == result["content"]
        assert streamed


def test_load_test_without_streaming_has_no_delta_events(client):
    start = client.post(
        "/api/loadtest",
        json={
            "prompt": "quiet",
            "count": 2,
            "concurrency": 2,
            "max_tokens": 8,
            "temperature": 0.0,
            "stream": False,
        },
    )
    run_id = start.get_json()["id"]
    events = parse_sse(client.get(f"/api/loadtest/{run_id}/stream").get_data(as_text=True))

    assert not [name for name, _ in events if name == "delta"]
    assert len([name for name, _ in events if name == "result"]) == 2

