from __future__ import annotations

from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict
import yaml


class ServicePoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lanes: int
    timeout_seconds: int


class ServiceConfig(BaseModel):
    """Runtime configuration owned by the OpenCLI service."""

    model_config = ConfigDict(extra="forbid")
    host: str
    port: int
    default_profile: str
    default_lanes: int
    default_timeout_seconds: int
    poll_seconds: float
    pools: dict[str, ServicePoolConfig]


def load_service_config(path: Path | None = None) -> ServiceConfig:
    if path is None:
        default_config = resources.files("openfeed").joinpath(
            "opencli_service/default_config.yaml",
        )
        raw_text = default_config.read_text(encoding="utf-8")
        source = str(default_config)
    else:
        if not path.is_file():
            raise FileNotFoundError(f"OpenCLI service config does not exist: {path}")
        raw_text = path.read_text(encoding="utf-8")
        source = str(path)

    parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"OpenCLI service config must be a mapping: {source}")
    config = ServiceConfig.model_validate(parsed)
    if config.port <= 0 or config.port > 65535:
        raise ValueError(f"OpenCLI service port out of range: {config.port}")
    if config.default_lanes <= 0:
        raise ValueError("default_lanes must be positive")
    if config.default_timeout_seconds <= 0:
        raise ValueError("default_timeout_seconds must be positive")
    for site, pool in config.pools.items():
        if pool.lanes <= 0:
            raise ValueError(f"pools.{site}.lanes must be positive")
        if pool.timeout_seconds <= 0:
            raise ValueError(f"pools.{site}.timeout_seconds must be positive")
    return config
