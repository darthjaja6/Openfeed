# Architecture

openfeed is two independent loops working against shared state:

- **supply cycle**: discover → patrol → filter → queue (every 15 min)
- **consumer cycle**: push → collect feedback → learn (every 30 sec)

Each loop runs as its own daemon (`openfeed-supply-cycle` /
`openfeed-refill-cycle`). They communicate only through files in the instance
`output/state/` and `output/ledgers/`. Either can be stopped, restarted, or rerun without
breaking the other.

## Data model

```
state/                                  ledgers/
├── source_catalog/                     ├── decisions.jsonl       (admit/reject events)
│   ├── beauty.json                     ├── feedback.jsonl        (incoming engagement)
│   ├── AI.json                         ├── history.jsonl         (cards pushed)
│   └── ...                             └── cycle_summary.jsonl   (per-tick summaries)
├── queue.json
├── queue_status.json
├── search_terms.json
├── learn_state.json
├── feedback_state.json
├── video_cache_index.json
└── video_assets.json
```

Catalog is split per-topic so concurrent `openfeed-discover --topic <X>`
processes can write independently. Everything else is a single file.

## Phase responsibilities

### supply side

| phase           | input                                  | output                            |
|-----------------|----------------------------------------|-----------------------------------|
| **discover**    | `openfeed.yaml`, `search_terms.json`   | new entries in catalog            |
| **patrol**      | active sources from catalog            | per-item files under `queues/patrol/` |
| **filter**      | `queues/patrol/*.json`                 | items in `queue.json`             |
| **queue_manage**| `queue.json`                           | `queue_status.json` (signal file) |
| **prepare_video**| YouTube items in queue                | mp4 files in `state/video_cache/` |
| **cleanup_assets**| catalog + ticlawk asset list         | local + remote storage trimmed    |

### consumer side

| phase                 | input                          | output                            |
|-----------------------|--------------------------------|-----------------------------------|
| **push**              | `queue.json`, ticlawk metrics  | cards POSTed to producer; `history.jsonl` row per push |
| **collect_feedback**  | `feedback_state.json`, ticlawk | `feedback.jsonl` rows             |
| **learn**             | `feedback.jsonl`, catalog      | catalog posterior + status updates; `search_terms.json` keyword additions (LLM-proposed from positive feedback) and retirements |

## Key design choices (and why)

### Per-platform serialization
opencli drives a single Chrome instance through a daemon. Concurrent calls
race on tab reuse, and parallel hits to the same logged-in account trigger
anti-bot detection. We use a per-platform file lock (`fcntl.flock` on
`/tmp/openfeed-opencli-{platform}.lock`) so:

- Different platforms (YouTube + X + web) run in parallel
- Same platform serializes globally — across threads AND across processes
- No timeout coordination needed; producers just block on the lock

### Per-topic catalog files
A single `source_catalog.json` would force us to lock the file on every
write, blocking discover-per-topic concurrency. Per-topic files mean
`openfeed-discover --topic A` and `openfeed-discover --topic B` can run
simultaneously, each only writing its own `source_catalog/A.json` /
`source_catalog/B.json`. Consumer-side phases load all files once at
start.

### Two retire paths
Sources get retired by either:

1. **Bayesian retire** (learn-side): `posterior.mean < 0.4` AND
   `evidence_count >= 2` AND in topic's bottom-K. Triggered by real user
   feedback.
2. **Filter retire**: source patrolled returns items but filter admits 0
   for N consecutive ticks. Triggered by content-quality drift (channel
   pivots topic, stops shipping fresh content, etc.).

In both cases, queue items belonging to the retired source are pruned —
push doesn't keep flushing a known-bad source's stockpile.

### LLM judgment with persona
Every source-review and content-review LLM call gets:

- **persona** (`openfeed.yaml`'s `persona.demographics`): who is
  being judged for. User-written, immutable across runs.

Persona keeps the LLM anchored on user taste. Drift in what the user
actually likes per topic is captured separately, by `learn` expanding the
per-topic search-keyword pool from positive feedback (so discover surfaces
more of the same kind of content) — not by mutating the prompt.

## Ledger semantics

`ledgers/*.jsonl` are append-only. Anything in `state/` can be
reconstructed from ledgers + `openfeed.yaml` (in principle). Workflow is:

1. New event happens → ledger row first
2. Atomic-write the affected `state/*.json`
3. If the state write fails, next tick's idempotent code re-applies from
   the ledger

This means you can wipe `state/` after a bad migration and replay from
ledgers to recover.

## Failure isolation

- LLM down → discover, filter, learn each pause their LLM phase; non-LLM
  work continues
- opencli down → patrol returns errors per source, filter still drains
  the patrol queue
- ticlawk down → push fails with rendered cards still cached in
  `queue.json` for next tick
- Worker crash mid-tick → atomic writes + ledger means state is
  consistent; next tick replays from cursor

## Further reading

- [`operations.md`](operations.md) — production operations and recovery notes
- [`docs/runtime-config.md`](runtime-config.md) — every `runtime` field in `openfeed.yaml`
- [`docs/custom-producer.md`](custom-producer.md) — writing your own feed
  producer
