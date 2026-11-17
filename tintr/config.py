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
    concurrency: int = 10  # parallel VLM requests (anchors and keys of all clips share one pool)
    # anchor: the model decides every part alone, so a strong reasoning model pays off (one call per layer)
    vlm_model: str = "anthropic/claude-opus-5.5"  # best in the anchor test (with details 40.7/42, ~$0.09, ~30 s)
    vlm_est_cost: float = 0.09  # rough $ per anchor for the estimate; real cost comes from fal usage
    vlm_reasoning: bool = True  # anchor reasoning; models that require it get it anyway
    # keys that follow the approved anchor: a fast model is as accurate here (bench: 100% vs 100%)
    vlm_model_follow: str = "anthropic/claude-haiku-5.5"
    vlm_est_cost_follow: float = 0.002  # rough $ per followed key
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
