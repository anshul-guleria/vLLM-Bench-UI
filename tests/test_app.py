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


# ---------------------------------------------------------------------------
# Saved connections
# ---------------------------------------------------------------------------


def test_config_lists_the_seeded_connection(client):
    body = client.get("/api/config").get_json()

    assert body["profiles"][0]["model"] == "fake-model"
    assert body["active_id"] == body["profiles"][0]["id"]


def test_saving_a_new_connection_switches_to_it(client):
    saved = client.post(
        "/api/profiles",
        json={"base_url": "http://second:9000/v1/", "model": "other-model"},
    )

    assert saved.status_code == 201
    state = saved.get_json()
    assert state["model"] == "other-model"
    assert state["base_url"] == "http://second:9000/v1"
    assert len(state["profiles"]) == 2

    config = client.get("/api/config").get_json()
    assert config["model"] == "other-model"
    assert config["active_id"] == state["active_id"]


def test_activating_a_saved_connection_switches_back(client):
    first_id = client.get("/api/config").get_json()["profiles"][0]["id"]
    client.post("/api/profiles", json={"base_url": "http://second:9000/v1", "model": "other-model"})

    activated = client.post(f"/api/profiles/{first_id}/activate")

    assert activated.status_code == 200
    assert activated.get_json()["model"] == "fake-model"


def test_deleting_the_active_connection_falls_back(client):
    client.post("/api/profiles", json={"base_url": "http://second:9000/v1", "model": "other-model"})
    state = client.get("/api/config").get_json()
    active_id = state["active_id"]

    deleted = client.delete(f"/api/profiles/{active_id}")

    assert deleted.status_code == 200
    assert deleted.get_json()["model"] == "fake-model"
    assert len(deleted.get_json()["profiles"]) == 1


def test_last_connection_cannot_be_deleted(client):
    active_id = client.get("/api/config").get_json()["active_id"]

    response = client.delete(f"/api/profiles/{active_id}")

    assert response.status_code == 400


def test_bad_connection_input_is_rejected(client):
    assert client.post("/api/profiles", json={"base_url": "ftp://nope", "model": "m"}).status_code == 400
    assert client.post("/api/profiles", json={"base_url": "http://ok/v1", "model": " "}).status_code == 400


def test_connections_persist_across_app_restarts(fake, tmp_path):
    path = tmp_path / "profiles.json"
    first_app = create_app(
        Endpoint(base_url=fake.base_url, api_key="", model="fake-model", timeout=10),
        profiles_path=path,
    )
    first_client = first_app.test_client()
    first_client.post("/api/profiles", json={"base_url": "http://second:9000/v1", "model": "other-model"})

    second_app = create_app(
        Endpoint(base_url=fake.base_url, api_key="", model="fake-model", timeout=10),
        profiles_path=path,
    )
    body = second_app.test_client().get("/api/config").get_json()

    assert body["model"] == "other-model"
    assert len(body["profiles"]) == 2


