# Ticlawk Feed Example

This example reads from TikTok and pushes cards to Ticlawk via
`consumer_type: ticlawk`.

Copy `openfeed.yaml`, `.env.local.example`, and `run-openfeed` to your instance
root. Install Browser Bridge, log in to TikTok in Google Chrome, then set
`TICLAWK_PUBLISHER_API_KEY` and replace `your-ticlawk-channel-id` before
running OpenFeed.

Before starting the full producer, verify the Ticlawk publishing path:

```bash
./run-openfeed smoke
```

This sends one minimal HTML test card to the `consumer_config.channel_id` in
`openfeed.yaml`. It does not use OpenRouter, OpenCLI, Browser Bridge, Chrome,
source discovery, or media preparation.
