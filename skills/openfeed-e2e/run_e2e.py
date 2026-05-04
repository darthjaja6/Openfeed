"""End-to-end regression for the openfeed package.

Single-process, no live API calls, runs in seconds. Each check is a small
self-contained function returning (ok: bool, msg: str). The runner prints
results + total + exits non-zero on any failure so it can gate `git push`.

Skill invokes this directly: `uv run python skills/openfeed-e2e/run_e2e.py`
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("OPENFEED_CONFIG_FILE", str(REPO_ROOT / "config" / "openfeed.yaml.example"))


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------


CHECKS: list[tuple[str, Callable[[], None]]] = []


def check(name: str):
    """Decorator: register a function as a regression check. The function
    raises AssertionError (or any exception) on failure; runner catches +
    reports."""
    def deco(fn: Callable[[], None]) -> Callable[[], None]:
        CHECKS.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 1. Module imports
# ---------------------------------------------------------------------------


@check("module imports")
def _():
    """Every top-level openfeed module imports without error."""
    import openfeed.cli  # noqa: F401
    import openfeed.core.discover  # noqa: F401
    import openfeed.core.patrol  # noqa: F401
    import openfeed.core.filter  # noqa: F401
    import openfeed.core.learn  # noqa: F401
    import openfeed.core.learn_search_terms  # noqa: F401
    import openfeed.core.push  # noqa: F401
    import openfeed.core.collect_feedback  # noqa: F401
    import openfeed.core.queue_manage  # noqa: F401
    import openfeed.core.cleanup_assets  # noqa: F401
    import openfeed.core.prepare_video  # noqa: F401
    import openfeed.core.supply_cycle  # noqa: F401
    import openfeed.core.refill_cycle  # noqa: F401
    import openfeed.core.interest_bootstrap  # noqa: F401
    import openfeed.core.deep_dive  # noqa: F401
    import openfeed.smoke_publish  # noqa: F401
    import openfeed.status  # noqa: F401
    import openfeed.prompts.content_deep_diving  # noqa: F401
    import openfeed.utils.catalog_io  # noqa: F401
    import openfeed.utils.queue_io  # noqa: F401
    import openfeed.utils.cycle_summary  # noqa: F401
    import openfeed.utils.run_with_lock  # noqa: F401
    import openfeed.utils.logging_setup  # noqa: F401
    import openfeed.clients.content.opencli  # noqa: F401
    import openfeed.clients.content.youtube_download  # noqa: F401
    import openfeed.prompts.content_review  # noqa: F401
    import openfeed.prompts.source_review  # noqa: F401
    import openfeed.prompts.keyword_proposal  # noqa: F401


# ---------------------------------------------------------------------------
# 2. Config loading
# ---------------------------------------------------------------------------


@check("openfeed.yaml loads against current schema")
def _():
    from openfeed.models.runtime import load_runtime
    from openfeed.models.interests import load_interests

    cfg = load_runtime(Path.cwd())
    assert cfg.learn.score_share > 0
    assert cfg.filter.zero_admit_retire_threshold > 0
    assert cfg.learn.deep_dive_workers >= 1
    interests = load_interests(Path.cwd())
    assert interests.persona.get("demographics"), "persona.demographics missing in openfeed.yaml"
    assert interests.interests, "interests list is empty"


@check("supply_cycle topic_reconcile precedes bootstrap_missing")
def _():
    from openfeed.core.supply_cycle import _bootstrap_missing, _TASKS
    # topic_reconcile must run first so changed/new topics are reflected before
    # bootstrap_missing decides which topic needs a scoped onboarding pass.
    assert [name for name, _ in _TASKS[:2]] == ["topic_reconcile", "bootstrap_missing"], \
        f"unexpected supply prefix: {[name for name, _ in _TASKS[:2]]}"
    # Function exists + has the right signature (callable as task fn).
    assert callable(_bootstrap_missing)
    # We deliberately don't INVOKE it in e2e — when real catalog is missing
    # any topic it would kick off ~30min of LLM + opencli discover.


@check("consumer registry routes openfeed.yaml validation per-type")
def _():
    from openfeed.clients.consumer import CONSUMERS, get_consumer
    from openfeed.models.interests import InterestEntry

    # Registered: at minimum, ticlawk, local_web, and generic http
    assert "ticlawk" in CONSUMERS
    assert "local_web" in CONSUMERS
    assert "http" in CONSUMERS
    spec = get_consumer("ticlawk")
    assert spec.config_model.__name__ == "TiclawkConsumerConfig"
    local_spec = get_consumer("local_web")
    assert local_spec.config_model.__name__ == "LocalWebConsumerConfig"
    http_spec = get_consumer("http")
    assert http_spec.config_model.__name__ == "HttpConsumerConfig"

    # Valid InterestEntry passes
    entry = InterestEntry(
        topic="t", description="d", platforms={"youtube": {}},
        consumer_type="ticlawk",
        consumer_config={"channel_id": "ch_abc"},
        language_preferences=["English"],
    )
    assert entry.consumer_type == "ticlawk"

    # Wrong consumer_type → KeyError from get_consumer (wrapped in ValidationError)
    try:
        InterestEntry(
            topic="t", description="d", platforms={"youtube": {}},
            consumer_type="nonexistent",
            consumer_config={},
            language_preferences=["English"],
        )
    except Exception as exc:  # pydantic ValidationError wraps the KeyError
        assert "nonexistent" in str(exc)
    else:
        raise AssertionError("expected validation error for unknown consumer_type")

    # Wrong consumer_config (extra field) → ValidationError surfaced
    try:
        InterestEntry(
            topic="t", description="d", platforms={"youtube": {}},
            consumer_type="ticlawk",
            consumer_config={"channel_id": "ch_x", "rogue_field": "y"}, language_preferences=["English"],
        )
    except Exception as exc:
        assert "rogue_field" in str(exc) or "Extra" in str(exc)
    else:
        raise AssertionError("expected validation error for extra config field")


@check("openfeed doctor detects opencli doctor failure markers")
def _():
    from openfeed.doctor import _probe_reported_failure

    assert _probe_reported_failure("[MISSING] Extension: not connected")
    assert _probe_reported_failure("[FAIL] Connectivity: failed")
    assert not _probe_reported_failure("[OK] Daemon: running\n[OK] Connectivity: passed")


@check("local_web consumer stores cards and emits feedback changes")
def _():
    from openfeed.clients.consumer.local_web import (
        _append_event,
        get_channel_changes,
        get_channel_metrics,
        push_card,
    )

    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        try:
            record = push_card(
                channel_id="default",
                title="Local card",
                content_subtype="html",
                html="<h1>ok</h1>",
            )
            assert record["id"].startswith("local_")
            assert get_channel_metrics("default")["unconsumed_total"] == 1
            _append_event("default", {"card_id": record["id"], "event_type": "view"})
            _append_event(
                "default",
                {"card_id": record["id"], "event_type": "dwell", "dwell_seconds": 12},
            )
            page = get_channel_changes(channel_id="default", since="0")
            assert page["cursor"] == "2"
            assert len(page["changes"]) == 1
            change = page["changes"][0]
            assert change["deltas"]["views"] == 1
            assert change["current_distribution"]["p50_dwell_seconds"] == 12
        finally:
            os.chdir(cwd)


@check("generic http consumer talks OpenFeed protocol")
def _():
    import threading
    from http.server import ThreadingHTTPServer

    from openfeed.clients.consumer.http_consumer import (
        HttpConsumerConfig,
        fetch_changes,
        get_metrics,
        push_card,
    )
    from openfeed.clients.consumer.local_web import _Handler, _append_event

    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            cfg = HttpConsumerConfig(
                base_url=f"http://{host}:{port}",
                channel_id="default",
            )
            record = push_card(
                cfg,
                title="HTTP card",
                content_subtype="html",
                html="<h1>ok</h1>",
            )
            assert record["id"].startswith("local_")
            assert get_metrics(cfg)["unconsumed_total"] == 1
            _append_event("default", {"card_id": record["id"], "event_type": "view"})
            page = fetch_changes(cfg, since="0")
            assert page["cursor"] == "1"
            assert page["changes"][0]["deltas"]["views"] == 1
        finally:
            server.shutdown()
            server.server_close()
            os.chdir(cwd)


@check("smoke_publish selects ticlawk topic and emits minimal html card")
def _():
    from openfeed.models.interests import InterestEntry
    from openfeed.smoke_publish import _push_one, _select_topics

    tic = InterestEntry(
        topic="tic", description="d", platforms={"youtube": {}},
        consumer_type="ticlawk",
        consumer_config={"channel_id": "ch_tic"},
        language_preferences=["English"],
    )
    local = InterestEntry(
        topic="local", description="d", platforms={"youtube": {}},
        consumer_type="local_web",
        consumer_config={"channel_id": "default"},
        language_preferences=["English"],
    )

    selected = _select_topics([local, tic], topic=None, all_topics=False)
    assert [entry.topic for entry in selected] == ["tic"]
    assert _select_topics([local, tic], topic="tic", all_topics=False) == [tic]

    calls = []

    def fake_push_card(**kwargs):
        calls.append(kwargs)
        return {"id": "card_123"}

    record = _push_one(tic, push_card=fake_push_card)
    assert record["id"] == "card_123"
    assert calls[0]["channel_id"] == "ch_tic"
    assert calls[0]["content_subtype"] == "html"
    assert "OpenFeed smoke test" in calls[0]["html"]
    assert "ch_tic" in calls[0]["html"]


@check("push metrics fail closed and ticlawk normalizes cards_unread")
def _():
    from openfeed.clients.consumer import ticlawk
    from openfeed.core.push import _unconsumed_from_metrics

    assert _unconsumed_from_metrics({"unconsumed_total": 4}) == 4
    assert _unconsumed_from_metrics({"cards_unread": 3}) == 3
    assert _unconsumed_from_metrics({}) is None
    assert _unconsumed_from_metrics({"other": 0}) is None

    original = ticlawk._request
    try:
        ticlawk._request = lambda *args, **kwargs: {"data": {"cards_unread": 7}}  # type: ignore[assignment]
        metrics = ticlawk.get_channel_metrics("channel")
        assert metrics["unconsumed_total"] == 7
    finally:
        ticlawk._request = original  # type: ignore[assignment]


@check("feedback_state legacy single-cursor migrates to per-topic cursors")
def _():
    import json as _json
    from openfeed.core.collect_feedback import _load_state, _STATE_PATH

    # Backup any real state, write a legacy-shaped fake, restore at end.
    real_backup = _STATE_PATH.read_bytes() if _STATE_PATH.exists() else None
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            _json.dumps({"cursor": "legacy-cursor-xyz", "last_polled_at": "2026-04-25T00:00:00+00:00"}),
            encoding="utf-8",
        )
        state = _load_state(known_topics=["beauty", "AI"])
        assert state.cursors == {"beauty": "legacy-cursor-xyz", "AI": "legacy-cursor-xyz"}, \
            f"migration mismapped: {state.cursors}"
    finally:
        if real_backup is not None:
            _STATE_PATH.write_bytes(real_backup)
        else:
            _STATE_PATH.unlink(missing_ok=True)


@check("logging_setup attaches a RotatingFileHandler (idempotent)")
def _():
    import logging, tempfile
    from logging.handlers import RotatingFileHandler
    from openfeed.utils.logging_setup import configure_task_logging

    # Use a tmp dir so we don't mutate real logs/
    with tempfile.TemporaryDirectory() as td:
        path1 = configure_task_logging("e2e_check", log_dir=Path(td))
        path2 = configure_task_logging("e2e_check", log_dir=Path(td))
        assert path1 == path2, "log path should be deterministic"
        # Exactly one rotating handler attached for this path on root, despite 2 calls
        root = logging.getLogger()
        attached = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and Path(h.baseFilename).resolve() == path1
        ]
        assert len(attached) == 1, f"expected 1 rotating handler, got {len(attached)}"
        # llm_trace logger also got its rotating handler, propagate=False
        trace_logger = logging.getLogger("llm_trace")
        assert trace_logger.propagate is False
        trace_attached = [
            h for h in trace_logger.handlers
            if isinstance(h, RotatingFileHandler)
            and Path(h.baseFilename).name == "llm_trace_e2e_check.jsonl"
        ]
        assert len(trace_attached) == 1
        # Cleanup our test handlers so other checks don't see them
        for h in attached + trace_attached:
            (root if h in attached else trace_logger).removeHandler(h)
            h.close()


@check("get_user_profile reads persona + language_prefs from openfeed.yaml")
def _():
    from openfeed.models.user_profile import get_user_profile

    profile = get_user_profile(Path.cwd())
    assert profile.persona is not None
    assert profile.persona.demographics, "persona.demographics is empty"
    # extra="ignore" should silently drop any legacy `topic_guidance` block
    assert not hasattr(profile, "topic_guidance"), \
        "UserProfile.topic_guidance should no longer be a field"
    # language_preferences moved to per-topic on InterestEntry; not on UserProfile
    assert not hasattr(profile, "language_preferences"), \
        "language_preferences should no longer be on UserProfile (now per-topic)"
    # Round-trip: persona should match openfeed.yaml byte-for-byte (no LLM munging)
    from openfeed.models.interests import load_interests
    cfg = load_interests(Path.cwd())
    assert profile.persona.demographics == cfg.persona["demographics"], \
        "get_user_profile drifted from openfeed.yaml — should be a pure pass-through"


# ---------------------------------------------------------------------------
# 3. catalog_io round-trip
# ---------------------------------------------------------------------------


@check("catalog_io: save splits per-topic, load merges back")
def _():
    from openfeed.models.source import (
        BayesianPosterior, SourceAttribution, SourceCatalog, SourceEntry,
    )
    from openfeed.utils import catalog_io

    def mk(name: str, topic: str, status: str = "active") -> SourceEntry:
        return SourceEntry(
            source_id=name, platform="youtube", topic=topic, status=status,  # type: ignore
            name=name, url=f"https://yt/{name}",
            decision_reason_code="probe", decided_at="2026-04-25T00:00:00+00:00",
            attribution=SourceAttribution(introduced_at="2026-04-25T00:00:00+00:00"),
            posterior=BayesianPosterior(),
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cat = SourceCatalog(generated_at="2026-04-25T00:00:00+00:00", sources={
            "youtube:b1": mk("b1", "beauty"),
            "youtube:c1": mk("c1", "做菜美食"),
        })
        catalog_io.save_catalog(tmp, cat)
        files = sorted(p.name for p in (tmp / "source_catalog").glob("*.json"))
        assert files == ["beauty.json", "做菜美食.json"], f"unexpected: {files}"
        reloaded = catalog_io.load_catalog(tmp)
        # Per-topic key shape: `<platform>:<source_id>:<topic>`
        assert set(reloaded.sources.keys()) == {"youtube:b1:beauty", "youtube:c1:做菜美食"}


# ---------------------------------------------------------------------------
# 4. learn.score() — additive scoring per row
# ---------------------------------------------------------------------------


@check("learn.score() additive scoring matrix")
def _():
    from openfeed.core.learn import score
    from openfeed.models.feedback import FeedbackEntry
    from openfeed.models.runtime import load_runtime

    cfg = load_runtime(Path.cwd()).learn

    def fb(*, like=0, save=0, share=0, views=1, dwell=0.0, watch=0.0) -> FeedbackEntry:
        return FeedbackEntry(
            card_id="cid", content_id="vid", source_id="sid",
            topic="t", platform="youtube",
            observed_at="2026-04-25T00:00:00+00:00",
            last_consumed_at="2026-04-25T00:00:00+00:00",
            delta={"like_count": like, "save_count": save,
                   "share_count": share, "views": views},
            snapshot={"p50_dwell_seconds": dwell, "p50_watch_progress": watch,
                      "p90_dwell_seconds": dwell, "p90_watch_progress": watch},
        )

    # Active signals stack
    s = score(fb(like=1, save=1, share=1, dwell=70, watch=0.9), cfg)
    expected = (cfg.score_share + cfg.score_save + cfg.score_like
                + cfg.score_strong_positive)
    assert abs(s - expected) < 0.01, f"stacked score got {s}, expected {expected}"

    # Reflexive swipe
    s = score(fb(dwell=2, watch=0.0), cfg)
    assert s == -cfg.score_strong_negative, f"reflex got {s}"

    # Weak negative (short dwell + low watch)
    s = score(fb(dwell=8, watch=0.2), cfg)
    assert s == -cfg.score_weak_negative, f"weak neg got {s}"

    # No engagement signal in middle ground
    s = score(fb(dwell=20, watch=0.4), cfg)
    assert s == 0, f"middle ground got {s}, expected 0"

    # views=0 → no passive at all
    s = score(fb(like=1, views=0), cfg)
    assert s == cfg.score_like, f"views=0 should keep active only: {s}"


# ---------------------------------------------------------------------------
# 5. evaluate_retire — synthetic catalog
# ---------------------------------------------------------------------------


@check("catalog: same physical source independent across topics")
def _():
    """Per-topic catalog scoping — same channel in two topics is two
    independent entries, dedup is per-topic, retire of one leaves the
    other untouched."""
    from openfeed.models.source import (
        BayesianPosterior, SourceAttribution, SourceCatalog, SourceEntry,
    )
    from openfeed.utils import catalog_io

    def mk(topic: str, status: str = "active") -> SourceEntry:
        return SourceEntry(
            source_id="UCx", platform="youtube", topic=topic, status=status,  # type: ignore
            name=f"chan-in-{topic}", url="https://yt/UCx",
            decision_reason_code="probe",
            decided_at="2026-04-25T00:00:00+00:00",
            attribution=SourceAttribution(introduced_at="2026-04-25T00:00:00+00:00"),
            posterior=BayesianPosterior(),
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Same physical source UCx, two topics — should produce 2 independent entries.
        a = mk("AI")
        b = mk("startup", status="rejected")
        cat = SourceCatalog(generated_at="2026-04-25T00:00:00+00:00", sources={
            a.catalog_key: a,
            b.catalog_key: b,
        })
        catalog_io.save_catalog(tmp, cat)
        reloaded = catalog_io.load_catalog(tmp)
        assert "youtube:UCx:AI" in reloaded.sources
        assert "youtube:UCx:startup" in reloaded.sources
        assert reloaded.sources["youtube:UCx:AI"].status == "active"
        assert reloaded.sources["youtube:UCx:startup"].status == "rejected"
        # catalog_key property matches the dict key shape
        assert a.catalog_key == "youtube:UCx:AI"
        assert b.catalog_key == "youtube:UCx:startup"


@check("evaluate_retire: low-mean source flagged when ev>=min")
def _():
    from openfeed.core.learn import evaluate_retire
    from openfeed.models.runtime import load_runtime
    from openfeed.models.source import (
        BayesianPosterior, SourceAttribution, SourceCatalog, SourceEntry,
    )

    cfg = load_runtime(Path.cwd()).learn

    def mk(name, alpha, beta, ev, status="active") -> SourceEntry:
        return SourceEntry(
            source_id=name, platform="youtube", topic="t", status=status,  # type: ignore
            name=name, url=f"https://yt/{name}",
            decision_reason_code="x", decided_at="2026-04-25T00:00:00+00:00",
            attribution=SourceAttribution(introduced_at="2026-04-25T00:00:00+00:00"),
            posterior=BayesianPosterior(alpha=alpha, beta=beta),
            evidence_count=ev,
        )

    # Per-topic catalog keys: <platform>:<source_id>:<topic>
    cat = SourceCatalog(generated_at="now", sources={
        "youtube:bad:t":  mk("bad", 0.5, 5.0, 5),       # mean ~0.09, ev>=2 → retire
        "youtube:meh:t":  mk("meh", 1.0, 1.0, 1),       # mean 0.5, ev<2 → no
        "youtube:good:t": mk("good", 5.0, 0.5, 5),      # mean ~0.91 → no
    })
    retired = evaluate_retire(cat, cfg, decided_at="now")
    assert retired == ["youtube:bad:t"], f"got {retired}"
    assert cat.sources["youtube:bad:t"].status == "rejected"


# ---------------------------------------------------------------------------
# 6. evaluate_search_terms
# ---------------------------------------------------------------------------


@check("evaluate_search_terms: new parameterless rule")
def _():
    from openfeed.core.learn_search_terms import evaluate_search_terms
    from openfeed.models.source import (
        BayesianPosterior, SourceAttribution, SourceCatalog, SourceEntry,
    )

    def mk(name, term, status, reason) -> SourceEntry:
        return SourceEntry(
            source_id=name, platform="youtube", topic="t", status=status,  # type: ignore
            name=name, url=f"https://yt/{name}",
            decision_reason_code=reason,
            decided_at="2026-04-25T00:00:00+00:00",
            attribution=SourceAttribution(
                introduced_at="2026-04-25T00:00:00+00:00",
                introduced_by_seed_term=term,
            ),
            posterior=BayesianPosterior(),
        )

    cat = SourceCatalog(generated_at="now", sources={
        # SINGLE_LLM: 1 source, LLM-rejected → must retire (was: doesn't with old min_evidence=3)
        "youtube:s1": mk("s1", "SINGLE_LLM", "rejected", "off_topic"),
        # BAYESIAN: 2 sources, both Bayesian-retired → must retire (was: doesn't, posterior_below_threshold not in old whitelist)
        "youtube:b1": mk("b1", "BAYESIAN", "rejected", "posterior_below_threshold"),
        "youtube:b2": mk("b2", "BAYESIAN", "rejected", "posterior_below_threshold"),
        # FILTER: 1 source, filter_consistent_reject → must retire (same reason)
        "youtube:f1": mk("f1", "FILTER", "rejected", "filter_consistent_reject"),
        # ACTIVE: any active source means keep
        "youtube:a1": mk("a1", "ACTIVE", "active", "x"),
        "youtube:a2": mk("a2", "ACTIVE", "rejected", "off_topic"),
        # HARD_ONLY: 3 sources all hard-gate (low_subscribers) → must NOT retire (no real verdict)
        "youtube:h1": mk("h1", "HARD_ONLY", "rejected", "low_subscribers"),
        "youtube:h2": mk("h2", "HARD_ONLY", "rejected", "low_subscribers"),
        "youtube:h3": mk("h3", "HARD_ONLY", "rejected", "low_subscribers"),
        # MIXED: 1 hard-gate + 1 LLM-reject → judged set has only the LLM one → retire
        "youtube:m1": mk("m1", "MIXED", "rejected", "low_subscribers"),
        "youtube:m2": mk("m2", "MIXED", "rejected", "user_taste_mismatch"),
    })
    retire_map = evaluate_search_terms(cat)
    flat = sorted(t for terms in retire_map.values() for t in terms)
    assert flat == ["BAYESIAN", "FILTER", "MIXED", "SINGLE_LLM"], (
        f"expected [BAYESIAN, FILTER, MIXED, SINGLE_LLM], got {flat}"
    )


# ---------------------------------------------------------------------------
# 7. Prompt builders render expected sections
# ---------------------------------------------------------------------------


@check("content_review prompt builds with required sections")
def _():
    from openfeed.models.content_item import ContentItem, YouTubeDigest
    from openfeed.models.interests import InterestEntry
    from openfeed.models.persona import PersonaOutput
    from openfeed.prompts.content_review import build_content_review_prompt
    from openfeed.prompts.content_understanding import ContentUnderstanding

    item = ContentItem(
        content_id="x", source_id="y", topic="t", platform="youtube",
        fetched_at="2026-04-25T00:00:00+00:00", source_of="patrol",
        youtube=YouTubeDigest(title="hi", duration="0:30", views="100",
                              published="1d", url="https://yt/x", thumbnail_url=""),
    )
    msgs = build_content_review_prompt(
        content_item=item,
        content_understanding=ContentUnderstanding(understanding="u", language="en"),
        topic_data=InterestEntry(topic="t", description="d", platforms={"youtube": {}}, consumer_type="ticlawk", consumer_config={"channel_id": "ch_x"}, language_preferences=["English"]),
        persona=PersonaOutput(demographics="P"),
        language_preferences=["en"],
    )
    body = "\n".join(m["content"] for m in msgs)
    assert "Topic: t" in body
    assert "User persona: P" in body
    assert "Learned guidance" not in body, \
        "topic_guidance was removed; prompt should no longer mention it"


@check("source_review prompt builds with required sections")
def _():
    from openfeed.models.interests import InterestEntry
    from openfeed.models.persona import PersonaOutput
    from openfeed.prompts.source_review import build_source_review_prompt

    msgs = build_source_review_prompt(
        source_info="src", sample_understandings=[],
        topic_data=InterestEntry(topic="t", description="d", platforms={"youtube": {}}, consumer_type="ticlawk", consumer_config={"channel_id": "ch_x"}, language_preferences=["English"]),
        persona=PersonaOutput(demographics="P"),
        language_preferences=["en"],
    )
    body = "\n".join(m["content"] for m in msgs)
    assert "Topic: t" in body
    assert "Learned guidance" not in body, \
        "topic_guidance was removed; prompt should no longer mention it"


@check("keyword_proposal prompt has single new_keywords-list schema")
def _():
    from openfeed.prompts.keyword_proposal import (
        KeywordProposalUpdate, TopicExample, build_keyword_proposal_prompt,
    )
    from openfeed.models.persona import PersonaOutput

    schema_props = KeywordProposalUpdate.model_json_schema()["properties"]
    assert list(schema_props) == ["new_keywords"], f"schema fields: {list(schema_props)}"
    # Build a prompt and confirm structure
    msgs = build_keyword_proposal_prompt(
        topic="t",
        topic_description="TOPIC_DESC_MARKER",
        persona=PersonaOutput(demographics="P"),
        positive_examples=[TopicExample(content_id="vid1", title="vid",
                                        platform="youtube",
                                        discovered_by_keyword="anime dance")],
        active_keywords=["foo"],
        retired_keywords=["bar"],
        max_new_keywords=3,
    )
    assert len(msgs) == 3 and all("content" in m for m in msgs)
    body = "\n".join(m["content"] for m in msgs)
    assert "TOPIC_DESC_MARKER" in body, "topic_description not threaded into prompt"
    # Negative-example block must NOT appear (positive-only by design)
    assert "negative" not in body.lower(), "keyword_proposal must not feed negative examples"


@check("content_deep_diving schema + prompt builder")
def _():
    from openfeed.models.persona import PersonaOutput
    from openfeed.prompts.content_deep_diving import (
        ContentDeepDiving, build_content_deep_diving_prompt,
    )

    expected_fields = {"deep_dive"}
    schema_props = set(ContentDeepDiving.model_json_schema()["properties"].keys())
    assert schema_props == expected_fields, f"schema fields: {schema_props}"
    # `deep_dive` must be a plain string — i.e. not an object — so the LLM
    # can't sneak sub-keys (`reasoning`, `visual_details`, ...) under us.
    assert ContentDeepDiving.model_json_schema()["properties"]["deep_dive"]["type"] == "string"

    persona = PersonaOutput(demographics="P")
    # Text-only call shape (no images)
    msgs = build_content_deep_diving_prompt(
        text="Title: foo", topic="t", topic_description="td", persona=persona,
    )
    assert len(msgs) == 3 and msgs[0]["role"] == "system"
    # Topic + description + persona must all land in the user message
    body = msgs[1]["content"]
    body_lower = body.lower()
    assert ": t" in body_lower and "td" in body, f"topic+desc missing, body={body!r}"
    assert "user persona: p" in body_lower

    # Multimodal call shape (1 dummy image part)
    msgs2 = build_content_deep_diving_prompt(
        text="Title: foo", topic="t", topic_description="td", persona=persona,
        images=[b"\xff\xd8\xff\xd9"],  # 4-byte stub jpeg
    )
    user_parts = msgs2[1]["content"]
    assert isinstance(user_parts, list), "image call should produce list-of-parts user message"
    image_parts = [p for p in user_parts if p.get("type") == "image_url"]
    assert len(image_parts) == 1, f"expected 1 image part, got {len(image_parts)}"


@check("keyword_proposal renders rich block when TopicExample.perception is set")
def _():
    from openfeed.prompts.keyword_proposal import (
        TopicExample, build_keyword_proposal_prompt,
    )
    from openfeed.prompts.content_deep_diving import ContentDeepDiving
    from openfeed.models.persona import PersonaOutput

    rich = TopicExample(
        content_id="vid1", title="t", platform="youtube",
        discovered_by_keyword="orange cat nap",
        perception=ContentDeepDiving(
            deep_dive="An orange cat naps on an indoor sofa in soft afternoon light.",
        ),
    )
    plain = TopicExample(
        content_id="vid2", title="t2", platform="youtube",
        discovered_by_keyword=None,
    )
    msgs = build_keyword_proposal_prompt(
        topic="pets", topic_description="cute pet videos",
        persona=PersonaOutput(demographics="P"),
        positive_examples=[rich, plain],
        active_keywords=[], retired_keywords=[], max_new_keywords=3,
    )
    body = "\n".join(m["content"] for m in msgs)
    # Rich example expands into a deep_dive line + carries the discovery keyword
    assert "deep_dive : An orange cat naps on an indoor sofa" in body
    assert "discovered via search term: 'orange cat nap'" in body
    # Plain example stays single-line (no lineage paren when discovered_by_keyword is None)
    assert "2. [youtube] t2" in body
    assert "(source: " not in body, "old source_id rendering should be gone"


# ---------------------------------------------------------------------------
# 8. cycle_summary collector
# ---------------------------------------------------------------------------


@check("cycle_summary: add accumulates, flush writes one record")
def _():
    from openfeed.utils import cycle_summary

    # Redirect ledger to a temp path for this check
    original_path = cycle_summary.LEDGER_PATH
    with tempfile.TemporaryDirectory() as td:
        cycle_summary.LEDGER_PATH = Path(td) / "cycle_summary.jsonl"
        try:
            cycle_summary.add("phaseA", count=3, ok=True)
            cycle_summary.add("phaseA", new_field="x")     # merge
            cycle_summary.add("phaseB", n=10)
            cycle_summary.flush(cycle="test", tick_num=1,
                                started_at="2026-04-25T00:00:00+00:00", rc=0)
            lines = cycle_summary.LEDGER_PATH.read_text().splitlines()
            assert len(lines) == 1
            rec = json.loads(lines[0])
            assert rec["cycle"] == "test"
            assert rec["phases"]["phaseA"] == {"count": 3, "ok": True, "new_field": "x"}
            assert rec["phases"]["phaseB"] == {"n": 10}
            # Buffer reset after flush
            cycle_summary.flush(cycle="test2", tick_num=2,
                                started_at="2026-04-25T00:00:01+00:00", rc=0)
            lines = cycle_summary.LEDGER_PATH.read_text().splitlines()
            assert len(lines) == 2
            rec2 = json.loads(lines[1])
            assert rec2["phases"] == {}, "buffer not cleared between flushes"
        finally:
            cycle_summary.LEDGER_PATH = original_path


# ---------------------------------------------------------------------------
# 9. Runner
# ---------------------------------------------------------------------------


def main() -> int:
    t0 = time.perf_counter()
    passed = 0
    failed: list[tuple[str, str]] = []
    for name, fn in CHECKS:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc().strip().splitlines()
            short_tb = "\n      ".join(tb[-3:])
            print(f"  ✗ {name}: {exc}\n      {short_tb}")
            failed.append((name, str(exc)))

    elapsed = time.perf_counter() - t0
    total = len(CHECKS)
    print(f"\n{passed}/{total} passed in {elapsed:.2f}s")
    if failed:
        print(f"\nFAILED ({len(failed)}):")
        for name, msg in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
