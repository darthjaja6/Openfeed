# Operations

How to run openfeed in steady state, observe what it's doing, and recover
from common failures.

## Running daemons

Three foreground jobs make up the live system:

- **supply cycle** (`openfeed-supply-cycle`): patrol → filter →
  queue_manage → cleanup_assets. Default interval 15 min.
- **prepare media** (`openfeed-prepare-video`): keeps queued native video and
  image media ready in the local working set.
- **refill cycle** (`openfeed-refill-cycle`): push → collect_feedback →
  learn. Default interval 30 sec.

`openfeed-discover` is a one-shot — invoke manually when you want to
expand the source pool (or schedule it weekly).

The installed `openfeed` command is the recommended wrapper. It only resolves
the instance paths and starts the existing entrypoints; it does not replace the
engine modules.

### Recommended: cron

cron restarts each tick with a clean process. No drift, no leaked file
descriptors, no surprise hangs from a long-running interpreter.

```cron
# Supply: every 15 min — patrol + filter + queue + cleanup
*/15 * * * * openfeed --instance /path/to/openfeed-instance supply

# Refill: every minute — push + collect_feedback + learn
* * * * * openfeed --instance /path/to/openfeed-instance refill

# Prepare: every minute — keep local media ready for push
* * * * * openfeed --instance /path/to/openfeed-instance prepare

# Optional weekly discover (fresh source pool)
0 3 * * 1 openfeed --instance /path/to/openfeed-instance discover
```

(The default refill interval is 30s in `--loop` mode, but cron's minimum
is 1 min — that's fine for most users; bump cron to a `while true; do
... ; sleep 30; done` wrapper if you want sub-minute pushes.)

### Alternative: `--loop` + nohup

Quick for development:

```bash
openfeed --instance /path/to/openfeed-instance start
```

Use `openfeed --instance /path/to/openfeed-instance start --local-server --open`
when you also want the built-in local feed server.

## Observing

### What's happening right now

```bash
# Most recent tick of each cycle
tail -1 ledgers/cycle_summary.jsonl | jq '.'

# Per-topic source counts
for f in state/source_catalog/*.json; do
  jq -r '"\(.sources | to_entries | map(select(.value.status=="active")) | length) active in \(input_filename | sub("state/source_catalog/"; "") | sub(".json"; ""))"' "$f"
done

# Queue depth + topic breakdown
jq '.topics | to_entries | map({topic: .key, n: .value | length})' state/queue.json
```

### Recent admit / reject events

```bash
# Last 20 admits
grep '"event_type":"admit_source"' ledgers/decisions.jsonl | tail -20 | jq '.'

# Sources retired today
grep '"event_type":"retire_source"' ledgers/decisions.jsonl | jq -r 'select(.ts | startswith("'$(date -u +%Y-%m-%d)'"))'
```

### Per-tick activity over the last hour

```bash
# Aggregate cycle_summary entries
jq -s 'map(select(.started_at > "'$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)'")) | group_by(.cycle) | map({cycle: .[0].cycle, ticks: length, total_pushed: map(.phases.push.pushed // 0) | add, total_admitted: map(.phases.filter.admitted // 0) | add})' ledgers/cycle_summary.jsonl
```

## Recovery

### Daemon hung / stuck

```bash
# Find PID
pgrep -fa "openfeed .*start|openfeed.core.supply_cycle"
# Kill (SIGTERM lets the current tick complete)
pkill -TERM -f "openfeed .*start|openfeed.core.supply_cycle"
# Restart from cron / foreground runner
```

### opencli wedged

```bash
# Restart the opencli daemon
pkill -f "@jackwener/opencli"
# Next opencli call will re-spawn it
```

If you see persistent `BROWSER_CONNECT` / `Detached` errors, restart
Chrome itself.

### Catalog corrupted (pydantic errors on load)

```bash
# State is rebuildable from ledgers in principle. Practically:
# 1. Stop daemons
# 2. Move the bad per-topic file aside
mv state/source_catalog/{topic}.json state/source_catalog/{topic}.json.bad
# 3. Restart — discover --topic X will rebuild it on next run.
```

### "I made a config change and it's not taking"

Daemons load config at process start. Restart the affected foreground runner
or daemon after config changes.

## Cost monitoring

LLM calls happen in:

- discover: source review + content understanding (most expensive)
- filter: content review (per-item; bulk of recurring spend)
- learn: profile-update (rare; only when both sides hit min_examples)

```bash
# OpenRouter logs LLM calls; grep them locally
ls logs/llm_trace_*.jsonl | xargs wc -l
```

Set `LLM_BUDGET_USD_PER_DAY` if you want a hard cap (TODO: not enforced
yet).

## Disk

- `state/video_cache/` is the heaviest — `cache_max_gb` defaults to 5GB
  (LRU evict above that)
- `ledgers/*.jsonl` grow forever; rotate annually with logrotate or
  similar if you care
