"""Saved endpoint + model pairs for the connection switcher.

The .env values seed the first profile. After that the list lives in
profiles.json next to the app, so it survives restarts and browser changes.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


def normalize_url(url: str) -> str:
    return str(url).strip().rstrip("/")


@dataclass
class Profile:
    id: str
    base_url: str
    model: str

    def to_dict(self) -> dict:
        return asdict(self)


class ProfileStore:
    def __init__(self, path: Path | None, seed: tuple[str, str]):
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self._profiles: list[Profile] = []
        self._active_id = ""
        self._load(seed)

    # -- reads ---------------------------------------------------------

    @property
    def profiles(self) -> list[Profile]:
        with self.lock:
            return list(self._profiles)

    @property
    def active(self) -> Profile:
        with self.lock:
            for profile in self._profiles:
                if profile.id == self._active_id:
                    return profile
            return self._profiles[0]

    # -- writes --------------------------------------------------------

    def upsert(self, base_url: str, model: str) -> Profile:
        url = normalize_url(base_url)
        model = str(model).strip()
        with self.lock:
            for profile in self._profiles:
                if profile.base_url == url and profile.model == model:
                    self._active_id = profile.id
                    self._save()
                    return profile
            profile = Profile(uuid.uuid4().hex[:8], url, model)
            self._profiles.append(profile)
            self._active_id = profile.id
            self._save()
            return profile

    def activate(self, profile_id: str) -> Profile:
        with self.lock:
            for profile in self._profiles:
                if profile.id == profile_id:
                    self._active_id = profile.id
                    self._save()
                    return profile
        raise KeyError(profile_id)

    def delete(self, profile_id: str) -> Profile:
        with self.lock:
            remaining = [p for p in self._profiles if p.id != profile_id]
            if len(remaining) == len(self._profiles):
                raise KeyError(profile_id)
            if not remaining:
                raise ValueError("at least one connection must remain")
            self._profiles = remaining
            if self._active_id == profile_id:
                self._active_id = remaining[0].id
            self._save()
            return self.active

    # -- persistence ---------------------------------------------------

    def _load(self, seed: tuple[str, str]) -> None:
        seeded = Profile("default", normalize_url(seed[0]), str(seed[1]).strip())
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                profiles = [
                    Profile(str(item["id"]), normalize_url(item["base_url"]), str(item["model"]))
                    for item in data.get("profiles", [])
                ]
                active_id = str(data.get("active_id") or "")
                if profiles and any(p.id == active_id for p in profiles):
                    self._profiles = profiles
                    self._active_id = active_id
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self._profiles = [seeded]
        self._active_id = seeded.id
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_id": self._active_id,
            "profiles": [p.to_dict() for p in self._profiles],
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
