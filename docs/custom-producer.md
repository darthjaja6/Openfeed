# Connecting a custom feed client

OpenFeed can push cards to any HTTP service that implements the OpenFeed
consumer protocol. You do not need to add Python code to this repository.

## Configure OpenFeed

Use the built-in generic HTTP consumer:

```yaml
consumer_type: http
consumer_config:
  base_url: http://127.0.0.1:8765
  channel_id: default
```

If your service needs bearer auth:

```yaml
consumer_type: http
consumer_config:
  base_url: https://feed.example.com
  channel_id: user-main
  api_key_env: MY_FEED_API_KEY
```

Set `MY_FEED_API_KEY` in `.env.local`.

## Default protocol

Implement these endpoints:

```text
POST /openfeed/v1/cards
GET  /openfeed/v1/channels/{channel_id}/metrics
GET  /openfeed/v1/channels/{channel_id}/changes?since={cursor}
```

`POST /openfeed/v1/cards` receives either JSON or multipart form data.

Fields:

- `channel_id`
- `card_type` (`content`)
- `content_subtype` (`html`, `video`, `gallery`, or `youtube_video`)
- `title`
- `html` for HTML cards
- `video` file for native video cards
- repeated `images` files for gallery cards
- `video_id` for legacy `youtube_video` cards
- optional `thumbnail` file

Return either:

```json
{"id": "card_123"}
```

or:

```json
{"data": {"id": "card_123"}}
```

`GET /metrics` returns:

```json
{
  "unconsumed_total": 3
}
```

It may also return the same object inside a `data` envelope.

`GET /changes` returns:

```json
{
  "cursor": "next-cursor",
  "has_more": false,
  "changes": [
    {
      "card_id": "card_123",
      "deltas": {
        "like_count": 1,
        "save_count": 0,
        "share_count": 0,
        "views": 1
      },
      "current_distribution": {
        "p50_dwell_seconds": 12,
        "p90_dwell_seconds": 31,
        "p50_watch_progress": 0.4,
        "p90_watch_progress": 0.9
      },
      "last_consumed_at": "2026-05-03T12:00:00+00:00"
    }
  ]
}
```

This may also be wrapped in `{"data": ...}`. OpenFeed writes these changes to
`ledgers/feedback.jsonl`; `learn` consumes that ledger and does not care which
consumer produced the feedback.

## Existing APIs

Your service does not need to use the default paths. Map endpoint paths in
`openfeed.yaml`:

```yaml
consumer_type: http
consumer_config:
  base_url: https://ticlawk.com
  channel_id: ch_xxx
  api_key_env: TICLAWK_PUBLISHER_API_KEY
  endpoints:
    push_card: /api/cards
    get_metrics: /api/channels/{channel_id}/metrics
    fetch_changes: /api/channels/{channel_id}/changes
```

OpenFeed appends `?since=<cursor>` to `fetch_changes` when the path does not
include `{since}`.

## Built-ins

- `local_web`: zero-account local browser UI, used by the Quickstart.
- `http`: generic HTTP protocol adapter for custom clients.
- `ticlawk`: first-party shortcut for Ticlawk's existing API.

The card renderer name is an internal runtime default. It currently defaults to
`ticlawk` because those renderers produce the shared `CardPayload` shape used by
all current consumers.
