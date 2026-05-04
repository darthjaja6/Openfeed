"""Ticlawk card producer — exports a singleton `PRODUCER` for the registry.

HTML templates are user-owned instance files configured per topic/platform in
`openfeed.yaml`. The producer owns the platform dispatch and data preparation.
"""
from openfeed.card_producers.ticlawk.producer import TiclawkProducer

PRODUCER = TiclawkProducer()

__all__ = ["PRODUCER"]
