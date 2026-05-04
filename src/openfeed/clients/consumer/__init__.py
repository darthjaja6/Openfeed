"""Consumer-side adapter registry.

Each topic in `openfeed.yaml` declares a `consumer_type` (e.g.
`"local_web"`, `"http"`, or `"ticlawk"`) and a `consumer_config` dict. This module is the discovery
mechanism that routes those declarations to a concrete adapter.

Concrete adapters (`clients/consumer/<name>.py`) self-register by
appending to `CONSUMERS` at import time:

    from openfeed.clients.consumer import CONSUMERS, ConsumerSpec
    CONSUMERS["ticlawk"] = ConsumerSpec(
        config_model=TiclawkConsumerConfig,
        push_card=...,
        get_metrics=...,
        fetch_changes=...,
    )

Callers (push / collect_feedback) look up the spec by `consumer_type`,
validate the topic's raw `consumer_config` dict against
`spec.config_model`, then invoke the bound functions with the typed
config object plus call-specific args.

To connect a custom client without changing OpenFeed code, use
`consumer_type: http` and implement the OpenFeed consumer HTTP protocol.
Only first-party / built-in consumers need Python modules here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel


@dataclass(frozen=True)
class ConsumerSpec:
    """Bundle of one consumer's pydantic config + the four operations
    every consumer must implement.

    Each callable takes the validated config object as its first arg, then
    operation-specific args. Return shapes are consumer-specific (callers
    use `consumer_type` to switch on response handling)."""
    config_model: type[BaseModel]
    push_card: Callable[..., dict[str, Any]]
    get_metrics: Callable[..., dict[str, Any]]
    fetch_changes: Callable[..., dict[str, Any]]


CONSUMERS: dict[str, ConsumerSpec] = {}


def get_consumer(consumer_type: str) -> ConsumerSpec:
    """Look up a registered consumer or raise."""
    spec = CONSUMERS.get(consumer_type)
    if spec is None:
        known = sorted(CONSUMERS.keys())
        raise KeyError(
            f"unknown consumer_type {consumer_type!r}; registered: {known}"
        )
    return spec


# Side-effect imports: each adapter module registers itself at import time.
# Listed here so `from openfeed.clients.consumer import ...` triggers
# registration even when callers reach for the module directly.
from openfeed.clients.consumer import ticlawk  # noqa: E402,F401
from openfeed.clients.consumer import local_web  # noqa: E402,F401
from openfeed.clients.consumer import http_consumer  # noqa: E402,F401
