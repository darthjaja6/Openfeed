# Contributing to openfeed

Thanks for considering a contribution. openfeed is a personal-use project
that benefits from outside eyes — bug reports, doc fixes, additional feed
producers, and platform plugins are all welcome.

## Before you start

1. Open an issue first for non-trivial work (new platform, new producer,
   architecture changes). It's a 5-minute conversation that saves a 5-day
   PR rewrite.
2. Skim [`docs/architecture.md`](docs/architecture.md) so you understand the
   supply / consumer split. Most "this seems weird" moments resolve once you
   see the cycle structure.

## Setup

See the [Quick start](README.md#quick-start) in README. tl;dr:

```bash
git clone https://github.com/<your-fork>/openfeed.git
cd openfeed
uv venv && source .venv/bin/activate
uv pip install -e .
playwright install chromium
cp config/interests.yaml.example config/interests.yaml
```

## Running the tests

There's one regression suite. **Run it before every push:**

```bash
uv run python skills/openfeed-e2e/run_e2e.py
```

It's fast (under a second), all in-process, no network. If it fails, fix
the failure or add a new check that documents the new shape.

When you add a feature that touches:

- a new schema field → add a check that loads it
- a new prompt section → add a check that the prompt builder includes it
- a new state file → add a check that round-trips it in a tempdir
- a new ledger event → add a check that `cycle_summary.add(...)` records it

The e2e is a living regression — it's the gate, but it's also documentation
of "what this codebase expects to be true."

## Code style

- **KISS — don't over-engineer.** Minimal necessary code, no premature
  abstractions, no factories for things that don't need them. Three
  similar lines beats a premature abstraction.
- **Business code shouldn't know about plumbing.** Cross-cutting concerns
  (logging, tracing) go through global named loggers — never as parameters
  on business function signatures.
- **Explicit > implicit.** Prefer whitelisting (`include={...}`) over
  blacklisting. Pass typed pydantic objects, not exploded primitives.
- **Decouple concerns.** Don't conflate discovery with sampling, or LLM
  perception ("what is this") with LLM judgment ("does it fit"). Hard
  rules belong in code, not in LLM prompts.
- **Persist after every expensive step.** Each costly stage gets its own
  state file with `file existence = step done` semantics, so a crash
  never wastes work.
- **Find root causes; don't paper over.** When results look wrong,
  inspect the actual data — don't retry / guess / bury behind
  `try/except`.

## Commits

- One logical change per commit
- Imperative subject under 70 chars: `feat(filter): drop sources after N
  zero-admit ticks`
- Explain *why* in the body when the *what* isn't obvious
- Reference an issue / PR when applicable

## Pull requests

- Branch off `main`
- Run `skills/openfeed-e2e/run_e2e.py` and confirm it passes
- Squash WIP commits before requesting review
- In the PR body, include:
  - what changed and why (1-2 paragraphs)
  - any user-visible config or schema changes
  - link to the issue if there is one

## Things we'd love help with

- **Alternative card producers** — RSS, Atom, plain JSON, Discord, etc.
  See [`docs/custom-producer.md`](docs/custom-producer.md). Path-of-least-
  resistance is path 2 (native plugin) since it forces the producer
  abstraction we want.
- **A second LLM backend** — Anthropic / direct Google API / local. The
  `LLMRunner` Protocol in [`src/openfeed/clients/llm.py`](src/openfeed/clients/llm.py)
  is the contract.
- **Platform plugins** — adding a new platform (TikTok, Bluesky, Reddit)
  currently means touching ~5 files. A proper adapter shape would
  formalize this. Open an issue to align before coding.
- **Cost monitoring** — `LLM_BUDGET_USD_PER_DAY` enforcement isn't there
  yet. Lots of design surface here.
- **Doc fixes** — if you set up openfeed and something in the README /
  docs doesn't match reality, that's a bug. PR welcome.

## Code of conduct

Be kind, be specific, assume good faith. If a discussion gets heated, walk
away and come back. We're all here to make our feeds better.
