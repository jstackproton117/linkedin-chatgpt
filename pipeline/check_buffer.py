#!/usr/bin/env python3
"""Verify the Buffer setup and discover the ids the pipeline needs.

Run:  .venv/bin/python check_buffer.py

Read-only against Buffer (nothing is posted). Steps:
  1. buffer_config.json exists and has a key
  2. the key is accepted (account + organizations)
  3. a LinkedIn channel exists — its ids are written back into the config
  4. introspect the Post type: does a sent post expose status / sent time /
     the LinkedIn permalink? (the docs are silent; this settles it)
  5. list recent sent posts on that channel with whatever metrics exist
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer_client import Buffer, BufferError, load_config, save_config, metrics_to_dict  # noqa: E402


def fail(msg, fix=None):
    print(f"  FAIL  {msg}")
    if fix:
        print(f"        -> {fix}")
    sys.exit(1)


print("Buffer setup check")
print("=" * 60)

try:
    cfg = load_config()
except BufferError as e:
    fail(str(e))
if not cfg:
    fail("buffer_config.json missing or api_key not set",
         "cp buffer_config.example.json buffer_config.json && chmod 600 buffer_config.json, "
         "then paste the key from buffer.com -> Settings -> API")
print("  OK    config present, key is set")

b = Buffer(cfg)
try:
    acct = b.account()
except BufferError as e:
    fail(str(e))
orgs = acct.get("organizations") or []
print(f"  OK    key accepted — account {acct.get('id')} with {len(orgs)} organization(s)")
for o in orgs:
    print(f"          - {o.get('name')} ({o.get('id')})")

try:
    li = b.linkedin_channels()
except BufferError as e:
    fail(str(e))
if not li:
    fail("no LinkedIn channel connected in Buffer",
         "In Buffer: Channels -> Connect -> LinkedIn -> pick your PROFILE (not a Page), then re-run")
print(f"  OK    {len(li)} LinkedIn channel(s):")
for ch in li:
    print(f"          - {ch.get('name')}  id={ch.get('id')}  org={ch.get('organizationName')}")

chosen = None
if cfg.get("channel_id"):
    chosen = next((c for c in li if c["id"] == cfg["channel_id"]), None)
if not chosen:
    chosen = li[0]
    if len(li) > 1:
        print(f"  NOTE  several LinkedIn channels — using the first ({chosen.get('name')}). "
              "Edit channel_id in buffer_config.json to pick another.")
changed = (cfg.get("organization_id") != chosen["organizationId"]
           or cfg.get("channel_id") != chosen["id"])
cfg["organization_id"] = chosen["organizationId"]
cfg["channel_id"] = chosen["id"]
cfg["channel_name"] = chosen.get("name")
if changed:
    save_config(cfg)
    print(f"  OK    wrote organization_id / channel_id for '{chosen.get('name')}' into buffer_config.json")
else:
    print(f"  OK    channel '{chosen.get('name')}' already configured")

print()
print("  Post type fields (introspection):")
try:
    fields = b.post_type_fields()
except BufferError as e:
    fields = []
    print(f"        introspection unavailable: {e}")
interesting = [f for f in fields if any(k in f.lower() for k in
               ("status", "sent", "permalink", "link", "url", "error", "published", "external"))]
print(f"        {len(fields)} fields; relevant: {interesting or '(none matched)'}")
# Scalars only — `error` is an object (PostPublishingError) and needs a
# sub-selection, so it is read by the sync script, not listed here.
extra = [f for f in ("status", "sentAt", "externalLink", "isCustomScheduled") if f in fields]

print()
print("  Recent sent posts on this channel:")
try:
    sent = b.posts(cfg["organization_id"], cfg["channel_id"], status="sent", first=10, extra_fields=extra)
except BufferError as e:
    sent = []
    print(f"        could not list: {e}")
if not sent:
    print("        none yet — Buffer only knows about posts it sends. That's expected on a new account.")
for p in sent:
    m = metrics_to_dict(p.get("metrics"))
    keys = ", ".join(f"{k}={v}" for k, v in list(m.items())[:6]) or "no metrics yet (first appear ~24h after sending)"
    extras = " ".join(f"{k}={p.get(k)}" for k in extra if p.get(k) is not None)
    print(f"        - {p.get('dueAt','')[:16]}  {(p.get('text') or '')[:48]!r}")
    print(f"            {keys}  {extras}")

print()
print("=" * 60)
print("  Ready. The pipeline can now schedule to Buffer and read metrics back.")
