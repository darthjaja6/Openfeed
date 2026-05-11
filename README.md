# Openfeed

OpenFeed is an LLM-powered personal recommendation system that finds
cross-platform internet content tailored to your interests and learns from your
feedback over time.

You describe the topics you care about. OpenFeed finds sources on YouTube, X,
TikTok, and the web; filters new content against your taste; pushes the
surviving cards to your feed; and learns from feedback over time.

## Quickstart

These steps help OpenFeed learn your initial preferences from TikTok and build
a local content feed for you. When setup is complete, you can view and interact
with the feed at `http://localhost:8765`.

The Quickstart pushes to the built-in localhost web feed. It uses TikTok as the
source platform, so you need Google Chrome, the Browser Bridge extension, and a
logged-in TikTok session.

### 1. Set up and install

Run these commands from the directory where you want your keys and OpenFeed's runtime
files to live. Keep the code checkout separate from those runtime files:

```bash
git clone https://github.com/darthjaja6/Openfeed.git openfeed

uv tool install ./openfeed

cp openfeed/examples/web/openfeed.yaml openfeed.yaml
cp openfeed/examples/web/.env.local.example .env.local
mkdir -p output
```

### 2. Connect Chrome to Opencli

Install opencli:

```bash
npm install -g @jackwener/opencli
```

Install the Browser Bridge Chrome extension:

1. Download the latest `opencli-extension-v{version}.zip` from
   <https://github.com/jackwener/opencli/releases>.
2. Unzip it.
3. Open `chrome://extensions` in Google Chrome.
4. Enable **Developer mode**.
5. Click **Load unpacked** and select the unzipped extension folder.

Then open Google Chrome and log in to TikTok. OpenFeed talks to OpenCLI through
its local OpenCLI service; `openfeed start` launches that service automatically.
To check it manually, run the service in one terminal:

```bash
openfeed opencli-service
```

Then run the doctor in another terminal:

```bash
openfeed doctor
```

When the Browser Bridge is connected and the OpenCLI service is reachable, the
OpenCLI extension should show:

![OpenCLI Browser Bridge connected](opencli_extension.png)

### 3. Configure OpenFeed

Open `openfeed.yaml` and replace the example topic and description with
something you want to watch. You can tune the rest later.

Then add your OpenRouter API key to `.env.local`:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

### 4. Start OpenFeed

Start the local scheduler and feed server:

```bash
openfeed start --local-server --open
```

`start` launches the OpenCLI service and runs `openfeed doctor` first. If config,
credentials, templates, or local tools are missing, it stops and tells you what
to fix. After that it runs the supply, prepare, and refill loops, then opens the local feed at
`http://127.0.0.1:8765/`. The first cards can take a while because OpenFeed
needs to find sources, review content, and build the queue.

## CLI Usage

The installed `openfeed` command runs an OpenFeed instance. An instance is a
directory that contains `openfeed.yaml` and `.env.local`; runtime state,
downloads, logs, and ledgers go under `output/`.

When you run `openfeed` from the instance directory, no path flags are needed:

```bash
openfeed doctor
openfeed opencli-service
openfeed status
openfeed start --local-server --open
```

From anywhere else, point the CLI at the instance:

```bash
openfeed --instance /path/to/my-openfeed doctor
openfeed --instance /path/to/my-openfeed status
openfeed --instance /path/to/my-openfeed start
```

The defaults are:

```text
--config  <instance>/openfeed.yaml
--workdir <instance>/output
```

You can override them when needed:

```bash
openfeed --instance /path/to/my-openfeed \
  --config /path/to/openfeed.yaml \
  --workdir /path/to/output \
  status
```

Common one-shot commands are:

```bash
openfeed --instance /path/to/my-openfeed supply
openfeed --instance /path/to/my-openfeed prepare
openfeed --instance /path/to/my-openfeed refill
openfeed --instance /path/to/my-openfeed discover
```

`openfeed opencli-service` exposes a local HTTP API on `127.0.0.1:19826` for
browser-backed OpenCLI jobs. Other local projects can submit browser jobs there
instead of starting competing OpenCLI tab sessions. The service is independent
from OpenFeed's feed/topic/card pipeline, accepts any OpenCLI-supported
`site`/`command`, and can be started standalone:

```bash
openfeed --workdir ~/.local/state/openfeed-opencli opencli-service
```

OpenCLI service scheduling has its own config file, separate from
`openfeed.yaml`. See [OpenCLI Service](docs/opencli-service.md) for capability
discovery, service config, HTTP API, and job format.

## Next Steps

### Change your Preference Settings

Edit `openfeed.yaml` to change your topic, description, language preferences,
or enabled platforms. The next `openfeed start` run reads the updated
config and reconciles topic state before collecting new content.

### Keep a long-running job

The benefit of turning Openfeed to a long-running job is that it can check your
content consumption status and continuously refill your feed. `openfeed start`
is the foreground runner. After the Quickstart works, use cron on a machine that 
should keep OpenFeed running:

```cron
*/15 * * * * openfeed --instance /path/to/my-openfeed supply
* * * * * openfeed --instance /path/to/my-openfeed prepare
* * * * * openfeed --instance /path/to/my-openfeed refill
0 3 * * 1 openfeed --instance /path/to/my-openfeed discover
```

### Push to Ticlawk

The Quickstart uses `examples/web` to show you how the system works, 
but it lacks the learning phase. To fully utilize the power of Openfeed,
you can push cards to Ticlawk. It exposes your content consumption stats
so that Openfeed can learn from it and adjust the content it finds for you,
just like a recommendation system.
To do this, follow [the Ticlawk example](examples/ticlawk/README.md).
The same consumer contract is described in
[Custom feed clients](docs/custom-producer.md).

## Advanced

- [Production operations](docs/operations.md)
- [OpenCLI Service](docs/opencli-service.md)
- [Internal runtime defaults](docs/runtime-config.md)
- [Custom feed clients](docs/custom-producer.md)
- [Architecture](docs/architecture.md)

## Contributing

The regression suite runs in GitHub Actions on pull requests and pushes to
`main`. Run the same check locally before submitting a change:

```bash
uv run python skills/openfeed-e2e/run_e2e.py
```

See `CONTRIBUTING.md` for the full contribution guide.

## License

Apache License 2.0. See `LICENSE`.
