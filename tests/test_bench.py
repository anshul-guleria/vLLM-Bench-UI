"""Behaviour tests for the benchmark engine, run against a real local HTTP server."""

import threading

from bench import Endpoint, chat, percentile, run_load_test, summarize


def endpoint(fake, timeout=10.0):
    return Endpoint(base_url=fake.base_url, model="fake-model", timeout=timeout)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_interpolates_between_neighbours():
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_percentile_handles_empty_and_single_samples():
    assert percentile([], 95) == 0.0
    assert percentile([42], 99) == 42.0


# ---------------------------------------------------------------------------
# A single call
# ---------------------------------------------------------------------------


def test_non_streaming_call_reports_content_usage_and_latency(fake):
    result = chat(endpoint(fake), [{"role": "user", "content": "hi"}], stream=False)

    assert result.ok is True
    assert result.content == "Hello from the fake"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 4
    assert result.ttft_ms is None
    assert result.latency_ms > 0
    assert result.tokens_per_sec > 0


def test_streaming_call_reports_time_to_first_token(fake):
    result = chat(endpoint(fake), [{"role": "user", "content": "hi"}], stream=True)

    assert result.ok is True
    assert result.content == "Hello from the fake"
    assert result.ttft_ms is not None and result.ttft_ms > 0
    assert result.ttft_ms <= result.latency_ms
    assert result.completion_tokens == 4


def test_server_error_is_recorded_not_raised(fake):
    result = chat(endpoint(fake), [{"role": "user", "content": "FAIL"}], stream=False)

    assert result.ok is False
    assert result.status == 500
    assert "engine exploded" in result.error


def test_unreachable_server_is_recorded_not_raised():
    dead = Endpoint(base_url="http://127.0.0.1:9/v1", model="m", timeout=2.0)
    result = chat(dead, [{"role": "user", "content": "hi"}], stream=False)

    assert result.ok is False
    assert result.error


# ---------------------------------------------------------------------------
# Load test
# ---------------------------------------------------------------------------


def test_load_test_runs_every_request_and_summarises(fake):
    results, wall_ms = run_load_test(
        endpoint(fake),
        prompt="count me",
        count=5,
        concurrency=2,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )

    assert len(results) == 5
    assert fake.hits == 5
    assert all(r.ok for r in results)
    assert wall_ms > 0

    summary = summarize(results, wall_ms)
    assert summary["total"] == 5
    assert summary["ok"] == 5
    assert summary["failed"] == 0
    assert summary["output_tokens"] == 20
    assert summary["latency"]["p50"] > 0
    assert summary["throughput_rps"] > 0
    assert summary["output_tps"] > 0


def test_load_test_actually_overlaps_requests(fake):
    fake.latency = 0.2
    results, wall_ms = run_load_test(
        endpoint(fake),
        prompt="in parallel",
        count=6,
        concurrency=6,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )

    assert all(r.ok for r in results)
    assert fake.max_active >= 3, "requests did not run concurrently"
    assert wall_ms < 6 * 200, "wall time looks serial"


def test_load_test_reports_progress_per_result(fake):
    seen = []
    run_load_test(
        endpoint(fake),
        prompt="progress",
        count=3,
        concurrency=2,
        max_tokens=8,
        temperature=0.0,
        stream=True,
        on_result=seen.append,
    )

    assert len(seen) == 3
    assert all(r.ttft_ms is not None for r in seen)


def test_load_test_streams_deltas_tagged_with_request_index(fake):
    deltas = {}

    def on_delta(index, text):
        deltas.setdefault(index, []).append(text)

    results, _ = run_load_test(
        endpoint(fake),
        prompt="stream me",
        count=2,
        concurrency=2,
        max_tokens=8,
        temperature=0.0,
        stream=True,
        on_delta=on_delta,
    )

    assert set(deltas) == {0, 1}
    for result in results:
        assert "".join(deltas[result.index]) == result.content
        assert result.content



def test_load_test_can_be_stopped_early(fake):
    fake.latency = 0.05
    stop = threading.Event()
    stop.set()

    results, wall_ms = run_load_test(
        endpoint(fake),
        prompt="stop",
        count=10,
        concurrency=2,
        max_tokens=8,
        temperature=0.0,
        stream=False,
        cancel=stop,
    )

    assert len(results) < 10


def test_summary_handles_mixed_failures(fake):
    results, wall_ms = run_load_test(
        endpoint(fake),
        prompt="FAIL please",
        count=3,
        concurrency=3,
        max_tokens=8,
        temperature=0.0,
        stream=False,
    )

    summary = summarize(results, wall_ms)
    assert summary["failed"] == 3
    assert summary["ok"] == 0
    assert summary["error_rate"] == 1.0
    assert summary["output_tokens"] == 0
