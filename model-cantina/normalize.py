"""Shared helper for building score records in a consistent shape.

Each sources/*.py module's normalize() function should build its records
with make_record() so every source produces the same dict shape for
storage.record_poll_results(), regardless of how different the raw payload
from that source looks.
"""


def make_record(name, category, score=None, score_type=None, org=None,
                 weight_availability=None, local_runnable=None,
                 modalities=None, raw_payload=None):
    return {
        "name": name,
        "org": org,
        "category": category,
        "score": score,
        "score_type": score_type,
        "weight_availability": weight_availability,
        "local_runnable": local_runnable,
        "modalities": modalities,
        "raw_payload": raw_payload,
    }
