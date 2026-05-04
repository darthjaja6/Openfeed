from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
import yaml


class ConfigFileError(RuntimeError):
    pass


def config_path() -> Path:
    value = os.environ.get("OPENFEED_CONFIG_FILE", "").strip()
    if not value:
        raise ConfigFileError("OPENFEED_CONFIG_FILE is required")
    path = Path(value).expanduser()
    if not path.is_file():
        raise ConfigFileError(f"OPENFEED_CONFIG_FILE does not exist: {path}")
    return path


def load_openfeed_config() -> dict:
    raw = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigFileError(f"openfeed config must be a mapping: {config_path()}")
    return raw


def load_env(workdir: Path) -> None:
    candidates: list[Path] = []
    configured = os.environ.get("OPENFEED_CONFIG_FILE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser().resolve().parent / ".env.local")
    candidates.append(Path.cwd() / ".env.local")
    candidates.append(workdir / ".env.local")

    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(resolved, override=False)
