"""Narrow Ticlawk publish smoke test.

This command proves the publisher credential + per-topic channel_id are enough
to create one Ticlawk HTML card. It intentionally does not load runtime config,
LLM clients, OpenCLI, source discovery, media preparation, or queue state.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable

from openfeed.clients.consumer.ticlawk import (
    TiclawkConsumerConfig,
    TiclawkError,
    push_card as ticlawk_push_card,
)
from openfeed.models.interests import InterestEntry, load_interests
from openfeed.utils.config_files import load_env


PushCard = Callable[..., dict]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_config(value: str | None) -> Path:
    raw = value or os.environ.get("OPENFEED_CONFIG_FILE") or "openfeed.yaml"
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"openfeed config not found: {path}")
    return path


def _resolve_workdir(value: str | None) -> Path:
    raw = value or os.environ.get("OPENFEED_WORKDIR") or "output"
    return Path(raw).expanduser().resolve()


def _ticlawk_topics(entries: list[InterestEntry]) -> list[InterestEntry]:
    return [entry for entry in entries if entry.consumer_type == "ticlawk"]


def _select_topics(
    entries: list[InterestEntry],
    *,
    topic: str | None,
    all_topics: bool,
) -> list[InterestEntry]:
    if topic and all_topics:
        raise ValueError("--topic and --all are mutually exclusive")
    if topic:
        matches = [entry for entry in entries if entry.topic == topic]
        if not matches:
            raise ValueError(f"topic not found in openfeed.yaml: {topic!r}")
        entry = matches[0]
        if entry.consumer_type != "ticlawk":
            raise ValueError(
                f"topic {topic!r} uses consumer_type={entry.consumer_type!r}; "
                "smoke publish only supports ticlawk"
            )
        return [entry]

    ticlawk_entries = _ticlawk_topics(entries)
    if not ticlawk_entries:
        raise ValueError("openfeed.yaml has no topic with consumer_type: ticlawk")
    return ticlawk_entries if all_topics else [ticlawk_entries[0]]


def _html(topic: str, channel_id: str) -> str:
    now = _utc_now_iso()
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>OpenFeed smoke test</title>"
        "<style>"
        "body{margin:0;padding:24px;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',sans-serif;background:#fff;color:#111;line-height:1.45}"
        "h1{font-size:24px;margin:0 0 12px}p{font-size:16px;margin:8px 0}"
        "code{background:#f2f2f2;border-radius:4px;padding:2px 5px}"
        "</style></head><body>"
        "<h1>OpenFeed smoke test</h1>"
        "<p>This card was published directly through the Ticlawk Publisher API.</p>"
        f"<p>Topic: <code>{escape(topic)}</code></p>"
        f"<p>Channel: <code>{escape(channel_id)}</code></p>"
        f"<p>Created at: <code>{escape(now)}</code></p>"
        "</body></html>"
    )


def _push_one(entry: InterestEntry, push_card: PushCard = ticlawk_push_card) -> dict:
    config = TiclawkConsumerConfig.model_validate(entry.consumer_config)
    return push_card(
        channel_id=config.channel_id,
        title=f"OpenFeed smoke test: {entry.topic}",
        content_subtype="html",
        html=_html(entry.topic, config.channel_id),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openfeed-smoke-publish")
    parser.add_argument(
        "--config",
        help="path to openfeed.yaml; defaults to OPENFEED_CONFIG_FILE or ./openfeed.yaml",
    )
    parser.add_argument(
        "--workdir",
        help="runtime output directory; defaults to OPENFEED_WORKDIR or ./output",
    )
    parser.add_argument("--topic", help="publish the smoke card to one topic's Ticlawk channel")
    parser.add_argument(
        "--all",
        action="store_true",
        help="publish one smoke card to every Ticlawk topic",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = _resolve_config(args.config)
        workdir = _resolve_workdir(args.workdir)
        os.environ["OPENFEED_CONFIG_FILE"] = str(config_path)
        os.environ["OPENFEED_WORKDIR"] = str(workdir)
        load_env(workdir)

        config = load_interests(workdir)
        selected = _select_topics(
            config.interests,
            topic=args.topic,
            all_topics=args.all,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"smoke setup failed: {exc}", file=sys.stderr)
        return 1

    ok = 0
    for entry in selected:
        try:
            record = _push_one(entry)
        except TiclawkError as exc:
            print(f"[FAIL] {entry.topic}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {entry.topic}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        card_id = record.get("id") or record.get("card_id") or "<unknown>"
        channel_id = entry.consumer_config.get("channel_id")
        print(f"[OK] topic={entry.topic} channel_id={channel_id} card_id={card_id}")
        ok += 1

    if ok == len(selected):
        print(f"Published {ok} Ticlawk smoke card(s).")
        return 0
    print(f"Published {ok}/{len(selected)} Ticlawk smoke card(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
