# Ticlawk Feed Example

This example uses `consumer_type: ticlawk` instead of the built-in local web
consumer used by the main Quickstart.

Start with [the main Quickstart setup](../../README.md#quickstart), but copy
`openfeed.yaml` and `.env.local.example` from this directory. Use the same
install, Chrome, TikTok, and OpenRouter steps from the main Quickstart. Set
`TICLAWK_PUBLISHER_API_KEY` in `.env.local` and replace
`your-ticlawk-channel-id` in `openfeed.yaml`.

Ticlawk-specific setup lives in
[ticlawk-public](https://github.com/darthjaja6/ticlawk-public#get-ticlawk-credentials).

Before starting the full producer, verify the Ticlawk publishing path:

```bash
openfeed smoke
```

This sends one minimal HTML test card to the `consumer_config.channel_id` in
`openfeed.yaml`. It does not use OpenRouter, OpenCLI, Browser Bridge, Chrome,
source discovery, or media preparation.
