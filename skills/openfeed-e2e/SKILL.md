---
name: openfeed-e2e
description: Run the openfeed regression test suite end-to-end before pushing changes to git or the OpenCLI fork. Verifies module imports, config loading, catalog I/O round-trip, learn scoring, retire logic, search-term retire, prompt builders, and cycle_summary collector. Activate when the user is about to commit or push, or asks to "test everything", "run the e2e", "check before push".
---

# openfeed e2e regression

Per-project regression harness for the openfeed codebase. **Always run this
before staging changes for git push** so we catch import-level breakage,
schema drift, and prompt-builder regressions early.

## When to invoke

- User asks to push / commit / open a PR — run this first as a gate
- User asks "test everything" / "run e2e" / "check before push" / "regression"
- After a multi-file refactor (e.g. catalog/io rename, prompt shape change)
- Before tearing down or restarting daemons that load the package

## How to run

```bash
uv run python skills/openfeed-e2e/run_e2e.py
```

Exit code 0 = all green; non-zero = failure summary printed at end. Each
check is independent and self-contained (uses tempdirs / synthetic data —
**never touches the live `state/` or `ledgers/` dirs**).

## What it covers

1. **Module imports** — every `openfeed.*` module loads cleanly
2. **Config loading** — `openfeed.yaml` runtime/interests + `get_user_profile()`
   parse against current pydantic schemas
3. **catalog_io round-trip** — save split, load merged, atomic per-topic write
4. **learn.score()** — synthetic feedback rows through the additive scoring
5. **evaluate_retire** — synthetic catalog → expected retire decisions
6. **evaluate_search_terms** — synthetic catalog → expected term retirements
7. **Prompt builders** — content_review + source_review + keyword_proposal
   prompts render with all expected sections (persona, examples, keyword pools)
8. **cycle_summary collector** — `add()` accumulates, `flush()` writes one record

If you add a new feature (catalog field, prompt section, retire rule, etc.),
add a check to `run_e2e.py`. Keep checks fast: target < 10 s total runtime.

## Reading failures

Each check prints `✓` on pass or `✗ <name>: <reason>` on fail. Final line
shows pass/total count. Failed checks block the push gate.

## What this skill does NOT do

- Doesn't hit real opencli / ticlawk / LLM APIs (slow + nondeterministic)
- Doesn't restart daemons or write to live state
- Doesn't run a full discover or patrol

For live-system smoke after deploy, use the manual `tmp/smoke_*.py` scripts.
