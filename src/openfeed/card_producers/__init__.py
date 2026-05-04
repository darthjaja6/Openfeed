"""Card producers — outer-boundary adapters between ContentItem and
a target feed platform's card format. One subdirectory per producer.

Peer to `clients/`. A producer owns render logic and exposes user-owned HTML
templates through `openfeed.yaml` where applicable.

Adding a new producer:
  1. Create `card_producers/<name>/` with producer.py
  2. Ensure its module's `PRODUCER` attribute is a `CardProducer` instance
  3. Register it in `_REGISTRY` in `base.get_producer`
  4. Set `runtime.push.producer: <name>` in openfeed.yaml
"""
