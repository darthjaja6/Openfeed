# Local Quickstart Example

This example is the fastest way to see OpenFeed push cards into the built-in
local web consumer. It does not need an app install, account registration, or
API key.

Run it from this directory:

```bash
./run-local-quickstart
```

The script seeds a small demo queue in `output/state/queue.json`, runs the real
`openfeed-push` task, then starts `openfeed-local-server` at
`http://127.0.0.1:8765/`.

Generated runtime files live under `output/` and are ignored by git. Re-running
the script resets the local demo inbox and pushes the demo cards again.

After interacting with the page, you can move local browser events into the
feedback ledger:

```bash
cd output
OPENFEED_CONFIG_FILE=../openfeed.yaml uv run --project ../../../.. openfeed-collect-feedback
```
