#!/usr/bin/env python3
"""Verify the Discord setup before trusting it in the daily job.

Run:  .venv/bin/python check_discord.py

Checks, in order, and tells you exactly what to fix at each step:
  1. discord_config.json exists and has a token
  2. the token is valid (who am I?)
  3. which servers the bot is actually in
  4. each configured channel is readable
  5. how many paper links it can currently see
"""
import json
import sys
from pathlib import Path

import requests

BASE = Path(__file__).parent
CFG = BASE / "discord_config.json"
API = "https://discord.com/api/v10"


def fail(msg, fix=None):
    print(f"  FAIL  {msg}")
    if fix:
        print(f"        -> {fix}")
    sys.exit(1)


print("Discord setup check")
print("=" * 60)

if not CFG.exists():
    fail("discord_config.json not found",
         "cp discord_config.example.json discord_config.json && chmod 600 discord_config.json")

cfg = json.loads(CFG.read_text(encoding="utf-8"))
token = (cfg.get("bot_token") or "").strip()
if not token or token.startswith("PASTE_"):
    fail("bot_token is not set",
         "Discord Developer Portal -> your app -> Bot -> Reset Token, paste it in")
print("  OK    config file present, token is set")

headers = {"Authorization": f"Bot {token}", "User-Agent": "rebel-intel/1.0"}

r = requests.get(f"{API}/users/@me", headers=headers, timeout=20)
if r.status_code == 401:
    fail("token rejected (401)", "The token is wrong or was regenerated. Reset it and re-paste.")
r.raise_for_status()
me = r.json()
print(f"  OK    authenticated as {me.get('username')}#{me.get('discriminator')} (id {me.get('id')})")

r = requests.get(f"{API}/users/@me/guilds", headers=headers, timeout=20)
r.raise_for_status()
guilds = r.json()
if not guilds:
    fail("the bot is not in any server",
         "OAuth2 -> URL Generator -> scopes:bot, perms: View Channels + Read Message History, "
         "then open the URL and pick a server where you have Manage Server")
print(f"  OK    bot is in {len(guilds)} server(s):")
for g in guilds:
    print(f"          - {g.get('name')} (id {g.get('id')})")

channels = cfg.get("channels") or []
if not channels:
    fail("no channels listed in discord_config.json",
         "Enable Developer Mode in Discord, right-click a channel -> Copy Channel ID")

print()
print("  Channel access:")
total_links = 0
ok_channels = 0
for ch in channels:
    cid = ch["id"] if isinstance(ch, dict) else ch
    name = ch.get("name", cid) if isinstance(ch, dict) else cid
    if str(cid).startswith("0000"):
        print(f"    SKIP  #{name} — placeholder id, replace it with a real channel id")
        continue
    r = requests.get(f"{API}/channels/{cid}/messages", headers=headers,
                     params={"limit": 100}, timeout=30)
    if r.status_code == 403:
        print(f"    FAIL  #{name} — 403, bot lacks View Channel / Read Message History here")
        continue
    if r.status_code == 404:
        print(f"    FAIL  #{name} — 404, wrong channel id or bot not in that server")
        continue
    if not r.ok:
        print(f"    FAIL  #{name} — HTTP {r.status_code}")
        continue
    msgs = r.json()
    import re
    rx = re.compile(r"arxiv\.org/(abs|pdf)/|huggingface\.co/papers/|openreview\.net/forum", re.I)
    hits = 0
    empty_content = 0
    for m in msgs:
        body = (m.get("content") or "")
        if not body:
            empty_content += 1
        for e in m.get("embeds") or []:
            body += " " + str(e.get("url") or "") + " " + str(e.get("title") or "")
        if rx.search(body):
            hits += 1
    ok_channels += 1
    total_links += hits
    note = ""
    if empty_content == len(msgs) and msgs:
        note = "  <-- all message bodies empty: enable MESSAGE CONTENT INTENT on the Bot tab"
    print(f"    OK    #{name} — {len(msgs)} recent message(s), {hits} with paper links{note}")

print()
print("=" * 60)
if ok_channels == 0:
    print("  No readable channels. Fix the errors above, then re-run.")
    sys.exit(1)
print(f"  Ready: {ok_channels} channel(s) readable, {total_links} paper link(s) visible right now.")
print("  Enable the feed by setting `enabled: true` on the Discord entry in")
print("  content_radar_config.yaml, then run: .venv/bin/python 01_fetch.py")
