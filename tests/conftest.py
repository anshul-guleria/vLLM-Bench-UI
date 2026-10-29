import pytest

from fakevllm import FakeVLLM


@pytest.fixture
def fake():
    server = FakeVLLM()
    yield server
    server.close()
