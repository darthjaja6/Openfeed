# OpenCLI Service

OpenFeed includes a local OpenCLI job service for browser-backed automation.
It is intentionally separate from OpenFeed's feed, topic, source, and card
pipeline: other local projects can use it as a shared browser job runner
without using OpenFeed's recommendation system.

The service owns one SQLite-backed job queue and a worker pool per site. It is
a generic OpenCLI runner, not an OpenFeed platform allowlist. Each job names
the OpenCLI adapter command to run:

```text
opencli <site> <command> <args...> --format json --reuse none
```

Default pools are only preconfigured concurrency settings for high-volume
sites:

```text
twitter  4 lanes
tiktok   4 lanes
youtube  4 lanes
google   5 lanes
```

Any other OpenCLI-supported site can still be submitted. If a site is not in
the pool config, the service starts it lazily with `default_lanes` and
`default_timeout_seconds`.

## Start

Install OpenFeed and OpenCLI, then start the service with a work directory for
its SQLite database and logs:

```bash
openfeed --workdir ~/.local/state/openfeed-opencli opencli-service
```

The service listens on `127.0.0.1:19826` by default. It does not require an
`openfeed.yaml` file when used standalone. If it is started from an OpenFeed
instance, it uses that instance's `output/` directory by default.

Useful options:

```bash
openfeed opencli-service --help
openfeed opencli-service --config ~/.config/openfeed/opencli-service.yaml
openfeed --workdir /tmp/opencli-service opencli-service --port 19827
```

## Configure

The service has its own config file. This file is not `openfeed.yaml` and does
not contain topics, channels, LLM settings, or feed publishing rules.

Example `opencli-service.yaml`:

```yaml
host: 127.0.0.1
port: 19826
default_profile: default
default_lanes: 1
default_timeout_seconds: 180
poll_seconds: 0.25
pools:
  twitter:
    lanes: 4
    timeout_seconds: 180
  tiktok:
    lanes: 4
    timeout_seconds: 180
  youtube:
    lanes: 4
    timeout_seconds: 180
  google:
    lanes: 5
    timeout_seconds: 120
```

If `--config` is omitted, the bundled service defaults are used. OpenFeed's
business config only needs to know the service URL through
`OPENFEED_OPENCLI_SERVICE_URL` when the service is managed elsewhere.

## Submit A Job

Submit a browser task:

```bash
curl -s -X POST http://127.0.0.1:19826/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "project": "my-agent",
    "site": "twitter",
    "command": "search",
    "args": ["AI agents", "--limit", "3"],
    "timeout_seconds": 180
  }'
```

The response contains a `job.id`. Poll the job:

```bash
curl -s http://127.0.0.1:19826/v1/jobs/job_...
```

Fetch the result:

```bash
curl -s http://127.0.0.1:19826/v1/jobs/job_.../result
```

The result payload includes `status`, `returncode`, parsed JSON `result`,
`stdout`, `stderr`, and `error`.

## Discover Capabilities

OpenCLI sites do not all expose the same commands. For example, one site may
have `search`, while another may only have `profile`, `read`, or `download`.
The source of truth is the OpenCLI registry, not OpenFeed.

Use the service capability endpoint when a caller needs to check whether a
site/command exists before submitting a job:

```bash
curl -s 'http://127.0.0.1:19826/v1/capabilities?site=twitter&command=search'
```

The endpoint returns OpenCLI registry entries from `opencli list -f json`,
including fields such as `site`, `name`, `command`, `description`, `strategy`,
`browser`, `args`, and `columns`.

Submitting an unsupported `site` or `command` is also valid from the service's
perspective. The job will run through OpenCLI and fail with the underlying
OpenCLI error. Use capability discovery for UX/preflight; use job failure
handling for the final authority.

## API

```text
GET  /v1/health
GET  /v1/pools
GET  /v1/capabilities[?site=twitter&command=search]
POST /v1/jobs
GET  /v1/jobs/{job_id}
GET  /v1/jobs/{job_id}/result
POST /v1/jobs/{job_id}/cancel
```

`POST /v1/jobs` accepts:

```json
{
  "project": "my-agent",
  "profile": "default",
  "site": "twitter",
  "command": "search",
  "args": ["AI agents", "--limit", "3"],
  "priority": 0,
  "timeout_seconds": 180,
  "idempotency_key": "optional-stable-key"
}
```

Fields:

- `project`: caller identity used for fair scheduling across local projects.
- `profile`: browser/OpenCLI profile label; omit for `default`.
- `site`: OpenCLI site adapter. This is not limited to OpenFeed-supported
  platforms; any OpenCLI-supported site can be submitted.
- `command`: adapter command for that site. Check `/v1/capabilities` or
  `opencli list -f json` when a caller needs to know whether a command exists.
- `args`: command arguments exactly as they would appear after
  `opencli <site> <command>`.
- `priority`: integer priority within the same site/profile pool.
- `timeout_seconds`: command execution timeout. It starts when a lane begins
  running the job, not while the job is waiting in the queue.
- `idempotency_key`: optional key for returning an existing queued/running/done
  job instead of inserting a duplicate.

Queued jobs can be cancelled. Running jobs are allowed to finish until the
service-owned command timeout expires; the service does not apply a separate
queue-wait timeout.

## Scheduling

Scheduling is scoped by `(profile, site)`. A Twitter job never blocks a TikTok
job unless the machine or Chrome itself is overloaded. Within one pool, the
service rotates across `project` values so one caller cannot permanently
monopolize all lanes. Restart recovery is conservative: jobs that were
`running` when the service exited are marked `failed` instead of automatically
rerun, because some OpenCLI tasks may perform writes.
