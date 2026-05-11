from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True)
class JobRequest:
    project: str
    site: str
    command: str
    args: list[str]
    profile: str
    priority: int
    timeout_seconds: int
    idempotency_key: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any], *, default_profile: str, default_timeout: int) -> "JobRequest":
        project = str(raw.get("project") or "default").strip()
        site = str(raw.get("site") or "").strip()
        command = str(raw.get("command") or "").strip()
        args_raw = raw.get("args") or []
        if not site:
            raise ValueError("site is required")
        if not command:
            raise ValueError("command is required")
        if not isinstance(args_raw, list):
            raise ValueError("args must be a list")
        args = [str(value) for value in args_raw]
        profile = str(raw.get("profile") or default_profile).strip() or default_profile
        priority = int(raw.get("priority") or 0)
        timeout_seconds = int(raw.get("timeout_seconds") or default_timeout)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        idempotency_key = raw.get("idempotency_key")
        return cls(
            project=project,
            site=site,
            command=command,
            args=args,
            profile=profile,
            priority=priority,
            timeout_seconds=timeout_seconds,
            idempotency_key=str(idempotency_key) if idempotency_key else None,
        )


@dataclass(frozen=True)
class PoolConfig:
    lanes: int
    timeout_seconds: int


@dataclass(frozen=True)
class Lane:
    profile: str
    site: str
    index: int
    timeout_seconds: int

    @property
    def id(self) -> str:
        return f"{self.profile}:{self.site}:{self.index}"

    @property
    def workspace(self) -> str:
        return f"opencli-service:{self.profile}:{self.site}:{self.index}"
