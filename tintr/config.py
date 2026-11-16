"""User config (%APPDATA%/Tintr/config.json) and API key storage (Windows Credential Manager)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import keyring

from tintr import APP_NAME

KEYRING_SERVICE = "Tintr-fal"
KEYRING_USER = "fal_api_key"


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    rpc_port: int = 3000
    concurrency: int = 4
    vlm_model: str = "google/gemini-3.8-flash"
    vlm_est_cost: float = 0.07  # rough $ per frame for the estimate; real cost comes from fal usage
    save_as_new_version: bool = True
    last_refs_dir: str = ""

    @classmethod
    def path(cls) -> Path:
        return app_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        p = cls.path()
        if p.exists():
            try:
                for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                    if hasattr(cfg, k):  # silently drops settings of removed features
                        setattr(cfg, k, v)
            except Exception:
                pass
        return cfg

    def save(self) -> None:
        self.path().write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")


def get_api_key() -> str:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:
        return ""


def set_api_key(key: str) -> None:
    key = key.strip()
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass
