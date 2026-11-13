"""Saved endpoint + model pairs, persisted next to the app."""

from pathlib import Path

from profiles import ProfileStore


def test_store_seeds_from_the_environment(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json", seed=("http://env:8000/v1", "env-model"))

    assert [p.model for p in store.profiles] == ["env-model"]
    assert store.active.base_url == "http://env:8000/v1"


def test_upsert_dedupes_normalised_urls_and_switches(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json", seed=("http://env:8000/v1", "env-model"))

    first = store.upsert("http://gpu:8000/v1/", "Qwen/Qwen3-0.6B")
    again = store.upsert("http://gpu:8000/v1", "Qwen/Qwen3-0.6B")

    assert first.id == again.id
    assert len(store.profiles) == 2
    assert store.active.id == first.id
    assert store.active.model == "Qwen/Qwen3-0.6B"


def test_upsert_same_url_different_model_creates_second_profile(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json", seed=("http://env:8000/v1", "env-model"))

    store.upsert("http://env:8000/v1", "other-model")

    assert len(store.profiles) == 2
    assert {p.model for p in store.profiles} == {"env-model", "other-model"}


def test_activate_and_delete_fall_back_to_a_remaining_profile(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json", seed=("http://env:8000/v1", "env-model"))
    second = store.upsert("http://gpu:8000/v1", "gpu-model")
    assert store.active.id == second.id

    store.delete(second.id)

    assert len(store.profiles) == 1
    assert store.active.model == "env-model"


def test_store_survives_a_restart(tmp_path):
    path = tmp_path / "profiles.json"
    first = ProfileStore(path, seed=("http://env:8000/v1", "env-model"))
    saved = first.upsert("http://gpu:8000/v1", "gpu-model")

    reopened = ProfileStore(path, seed=("http://env:8000/v1", "env-model"))

    assert reopened.active.id == saved.id
    assert {p.model for p in reopened.profiles} == {"env-model", "gpu-model"}


def test_corrupt_file_reseeds_instead_of_crashing(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{not json", encoding="utf-8")

    store = ProfileStore(path, seed=("http://env:8000/v1", "env-model"))

    assert store.active.model == "env-model"


def test_memory_store_writes_nothing(tmp_path):
    store = ProfileStore(None, seed=("http://env:8000/v1", "env-model"))
    store.upsert("http://gpu:8000/v1", "gpu-model")

    assert not list(Path(tmp_path).iterdir())
