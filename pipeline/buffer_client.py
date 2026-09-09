"""Buffer GraphQL client for Rebel Intel.

Buffer is the publishing and metrics layer for LinkedIn: it holds LinkedIn's
Member Post Analytics approval (which is not self-serve for individual apps),
so scheduling through Buffer is the shortest legitimate path to per-post
impressions, reach, reactions and comments for a personal profile.

The API key lives in buffer_config.json (gitignored, chmod 600) — the same
pattern as notify_config.json and discord_config.json. Never log it.

Docs: https://developers.buffer.com  (GraphQL, personal API key,
"available for personal workflows and automations only").
"""

import json
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "buffer_config.json"
ENDPOINT = "https://api.buffer.com"
TIMEOUT = 30


class BufferError(Exception):
    pass


def load_config():
    """Returns the config dict, or None when unconfigured (missing file or
    placeholder key). None is not an error: the daily job must run on a box
    that has no Buffer key."""
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise BufferError(f"buffer_config.json unreadable: {e}")
    key = (cfg.get("api_key") or "").strip()
    if not key or key.startswith("PASTE_"):
        return None
    return cfg


def save_config(cfg):
    """Persist discovered ids (organization, channel) next to the key."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def _lit(value):
    """Inline a value as a GraphQL literal. Buffer types its ids as custom
    scalars (OrganizationId!, ChannelId!, ...) rather than String!, so typed
    variables fail unless every scalar name is guessed right. Literals
    sidestep that: a custom scalar accepts a string literal. json.dumps
    produces a valid GraphQL string with correct escaping."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


class Buffer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.key = cfg["api_key"]
        self.org_id = cfg.get("organization_id")
        self.channel_id = cfg.get("channel_id")

    # ── transport ────────────────────────────────────────────────────────
    def gql(self, query, variables=None):
        resp = requests.post(
            ENDPOINT,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self.key}",
                     "User-Agent": "rebel-intel/1.0"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:
            raise BufferError("Buffer rejected the API key (401) — check buffer_config.json")
        if resp.status_code == 429:
            raise BufferError("Buffer rate limit (429) — try again later")
        try:
            body = resp.json()
        except ValueError:
            raise BufferError(f"Buffer returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
        if body.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
            raise BufferError(f"GraphQL error: {msgs}")
        return body.get("data") or {}

    # ── discovery ────────────────────────────────────────────────────────
    def account(self):
        return self.gql("query { account { id organizations { id name } } }")["account"]

    def channels(self, org_id):
        data = self.gql(
            f"query {{ channels(input: {{ organizationId: {_lit(org_id)} }}) {{ id name service }} }}")
        return data.get("channels") or []

    def linkedin_channels(self):
        """Every LinkedIn channel across all organizations on the account."""
        found = []
        for org in self.account().get("organizations") or []:
            for ch in self.channels(org["id"]):
                if (ch.get("service") or "").lower() == "linkedin":
                    found.append({**ch, "organizationId": org["id"], "organizationName": org.get("name")})
        return found

    def post_type_fields(self):
        """Introspect the Post type. The docs do not say whether a sent post
        exposes its status, send time or LinkedIn permalink; this finds out."""
        data = self.gql('query { __type(name: "Post") { fields { name type { name kind ofType { name } } } } }')
        t = data.get("__type") or {}
        return [f["name"] for f in (t.get("fields") or [])]

    # ── posts ────────────────────────────────────────────────────────────
    # Fields a caller can ask for on a Post beyond the basics. `error` is an
    # object, so it carries its own sub-selection. (From introspection —
    # the public docs do not list status, sentAt or externalLink at all.)
    POST_EXTRA = ("status", "sentAt", "externalLink", "isCustomScheduled",
                  "error { message rawError supportUrl }")

    def create_post(self, text, channel_id, due_at_iso=None, needs_approval=False):
        """Schedule a post on a channel.

        due_at_iso   ISO 8601 UTC. Pins the post to that time (customScheduled).
                     Without it Buffer takes the next free queue slot.
        needs_approval  True parks the post in Buffer as `needs_approval`, so
                     it will not go out until someone approves it in Buffer's
                     UI — a second gate on top of the dashboard's QA toggle.
        Returns the created post {id, status, dueAt, ...}.
        """
        parts = [f"text: {_lit(text)}", f"channelId: {_lit(channel_id)}",
                 "schedulingType: automatic"]
        if due_at_iso:
            parts += ["mode: customScheduled", f"dueAt: {_lit(due_at_iso)}"]
        else:
            parts += ["mode: addToQueue"]
        if needs_approval:
            parts += ["needsApproval: true"]
        q = f"""mutation {{
          createPost(input: {{ {', '.join(parts)} }}) {{
            __typename
            ... on PostActionSuccess {{ post {{ id status dueAt channelId isCustomScheduled }} }}
            ... on MutationError {{ message }}
          }}
        }}"""
        res = self.gql(q).get("createPost") or {}
        if res.get("__typename") != "PostActionSuccess" or not res.get("post"):
            reason = res.get("message") or json.dumps(res)[:300]
            raise BufferError(f"createPost failed ({res.get('__typename', '?')}): {reason}")
        return res["post"]

    def get_post(self, post_id, extra_fields=()):
        """One post with metrics. extra_fields lets the caller ask for
        whatever introspection said exists (status, sentAt, permalink...)."""
        extra = " ".join(extra_fields)
        q = f"""query {{
          post(input: {{ id: {_lit(post_id)} }}) {{
            id text dueAt channelId {extra}
            metrics {{ type name value unit }}
            metricsUpdatedAt
          }}
        }}"""
        return self.gql(q).get("post")

    def posts(self, org_id, channel_id, status="sent", first=20, extra_fields=()):
        extra = " ".join(extra_fields)
        q = f"""query {{
          posts(first: {int(first)}, input: {{
            organizationId: {_lit(org_id)},
            filter: {{ status: [{status}], channelIds: [{_lit(channel_id)}] }}
          }}) {{
            edges {{ node {{ id text dueAt channelId {extra}
                             metrics {{ type name value unit }} metricsUpdatedAt }} }}
            pageInfo {{ endCursor hasNextPage }}
          }}
        }}"""
        data = self.gql(q)
        return [e["node"] for e in (data.get("posts") or {}).get("edges") or []]


def metrics_to_dict(metrics):
    """Buffer returns [{type,name,value,unit}]. Flatten to {type: value}."""
    out = {}
    for m in metrics or []:
        key = (m.get("type") or m.get("name") or "").strip()
        if key:
            try:
                out[key] = float(m.get("value")) if m.get("value") is not None else None
                if out[key] is not None and out[key].is_integer():
                    out[key] = int(out[key])
            except (TypeError, ValueError):
                out[key] = m.get("value")
    return out
