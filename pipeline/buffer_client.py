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
SETTINGS_PATH = BASE / "settings.json"
USAGE_PATH = BASE / "data" / "buffer_usage.jsonl"
ENDPOINT = "https://api.buffer.com"
TIMEOUT = 30

# Buffer Free plan (developers.buffer.com/guides/api-limits): 100 requests
# per 15 min, 250 per 24 h, 3,000 per 30 days, one API key. The pipeline
# holds itself to a fraction of that — see `budget` in settings.json — so a
# bug or a runaway loop can never spend the month's quota. 429s do not
# consume quota, but we never want to see one.
PLAN_LIMITS = {"15min": 100, "24h": 250, "30d": 3000}
DEFAULT_BUDGET = {"daily_call_budget": 40, "monthly_call_budget": 600}


class BufferError(Exception):
    pass


class BudgetExceeded(BufferError):
    pass


def _budget():
    try:
        b = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("buffer", {})
    except Exception:
        b = {}
    return {k: int(b.get(k, v)) for k, v in DEFAULT_BUDGET.items()}


def usage_summary(now=None):
    """Calls recorded in the ledger over the last 24h and 30d, plus the most
    recent RateLimit header Buffer sent us."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    day = now - timedelta(hours=24)
    month = now - timedelta(days=30)
    n_day = n_month = 0
    last = None
    if USAGE_PATH.exists():
        for line in USAGE_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["at"])
            except Exception:
                continue
            if ts >= month:
                n_month += 1
                if ts >= day:
                    n_day += 1
                last = r
    b = _budget()
    # Buffer's own count, from the RateLimit header on the last call. It is
    # authoritative when present (it includes calls the ledger never saw,
    # e.g. before the ledger existed), so the budget check uses whichever of
    # ledger and header is higher. The header is missing on some requests.
    reported_24h = reported_30d = None
    last_seen = (last or {}).get("at")
    for item in (last or {}).get("ratelimit") or []:
        pol, rem = item.get("policy", ""), item.get("remaining")
        if rem is None:
            continue
        if "1day" in pol or "24h" in pol:
            reported_24h = PLAN_LIMITS["24h"] - rem
        elif "30day" in pol:
            reported_30d = PLAN_LIMITS["30d"] - rem
    # The header snapshot ages: if it is older than the window it says
    # nothing about the current window, so only trust a fresh one.
    fresh = False
    try:
        fresh = last_seen is not None and (now - datetime.fromisoformat(last_seen)) < timedelta(hours=24)
    except Exception:
        pass
    eff_24h = max(n_day, reported_24h or 0) if fresh else n_day
    eff_30d = max(n_month, reported_30d or 0) if fresh else n_month
    return {
        "calls_24h": eff_24h, "calls_30d": eff_30d,
        "ledger_24h": n_day, "ledger_30d": n_month,
        "reported_24h": reported_24h if fresh else None,
        "reported_30d": reported_30d if fresh else None,
        "daily_budget": b["daily_call_budget"], "monthly_budget": b["monthly_call_budget"],
        "plan_24h": PLAN_LIMITS["24h"], "plan_30d": PLAN_LIMITS["30d"],
        "last_ratelimit": (last or {}).get("ratelimit"),
        "last_call_at": last_seen,
    }


def _record_usage(op, status, ratelimit):
    from datetime import datetime, timezone
    USAGE_PATH.parent.mkdir(exist_ok=True)
    with USAGE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "op": op,
                            "status": status, "ratelimit": ratelimit}) + "\n")
    # Keep the ledger bounded: 30 days is all the budget math needs.
    try:
        lines = USAGE_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > 5000:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            keep = [l for l in lines if l and json.loads(l).get("at", "") >= cutoff]
            USAGE_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def _parse_ratelimit(header):
    """'"100-in-15min"; r=98; t=897' -> {'policy': '100-in-15min', 'remaining': 98, 'reset_s': 897}.
    Buffer may send several policies comma-separated; keep them all."""
    out = []
    for part in (header or "").split(","):
        part = part.strip()
        if not part:
            continue
        item = {"policy": part.split(";")[0].strip().strip('"')}
        for kv in part.split(";")[1:]:
            k, _, v = kv.strip().partition("=")
            if k in ("r", "t") and v.isdigit():
                item["remaining" if k == "r" else "reset_s"] = int(v)
        out.append(item)
    return out or None


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
    def gql(self, query, variables=None, op="query"):
        # Self-imposed budget, checked BEFORE the call. Interactive actions
        # (scheduling a post) surface this as an error the user can read;
        # the daily sync just stops for the day.
        u = usage_summary()
        if u["calls_24h"] >= u["daily_budget"]:
            raise BudgetExceeded(f"Buffer daily budget reached ({u['calls_24h']}/{u['daily_budget']} calls in 24h)")
        if u["calls_30d"] >= u["monthly_budget"]:
            raise BudgetExceeded(f"Buffer monthly budget reached ({u['calls_30d']}/{u['monthly_budget']} calls in 30d)")

        resp = requests.post(
            ENDPOINT,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self.key}",
                     "User-Agent": "rebel-intel/1.0"},
            timeout=TIMEOUT,
        )
        rl = _parse_ratelimit(resp.headers.get("RateLimit"))
        self.last_ratelimit = rl
        _record_usage(op, resp.status_code, rl)
        if resp.status_code == 401:
            raise BufferError("Buffer rejected the API key (401) — check buffer_config.json")
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            raise BufferError(f"Buffer rate limit (429) — retry after {retry}s")
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
        return self.gql("query { account { id organizations { id name } } }", op="account")["account"]

    def channels(self, org_id):
        data = self.gql(
            f"query {{ channels(input: {{ organizationId: {_lit(org_id)} }}) {{ id name service }} }}",
            op="channels")
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
        data = self.gql('query { __type(name: "Post") { fields { name type { name kind ofType { name } } } } }',
                        op="introspect")
        t = data.get("__type") or {}
        return [f["name"] for f in (t.get("fields") or [])]

    # ── posts ────────────────────────────────────────────────────────────
    # Fields a caller can ask for on a Post beyond the basics. `error` is an
    # object, so it carries its own sub-selection. (From introspection —
    # the public docs do not list status, sentAt or externalLink at all.)
    POST_EXTRA = ("status", "sentAt", "externalLink", "isCustomScheduled",
                  "error { message rawError supportUrl }")

    def create_post(self, text, channel_id, due_at_iso=None, needs_approval=False, mode=None):
        """Create a post on a channel.

        mode         ShareMode: shareNow (publish immediately), customScheduled
                     (needs due_at_iso), addToQueue (next free slot), shareNext.
                     Defaults to customScheduled when due_at_iso is given,
                     else addToQueue.
        due_at_iso   ISO 8601 UTC, used with customScheduled.
        needs_approval  True parks the post in Buffer as `needs_approval`, so
                     it will not go out until someone approves it in Buffer's
                     UI — a second gate on top of the dashboard's approval.
        Returns the created post {id, status, dueAt, ...}.
        """
        if mode is None:
            mode = "customScheduled" if due_at_iso else "addToQueue"
        if mode not in ("shareNow", "customScheduled", "addToQueue", "shareNext"):
            raise BufferError(f"unknown share mode {mode!r}")
        if mode == "customScheduled" and not due_at_iso:
            raise BufferError("customScheduled needs due_at_iso")
        parts = [f"text: {_lit(text)}", f"channelId: {_lit(channel_id)}",
                 "schedulingType: automatic", f"mode: {mode}"]
        if mode == "customScheduled":
            parts.append(f"dueAt: {_lit(due_at_iso)}")
        if needs_approval:
            parts += ["needsApproval: true"]
        q = f"""mutation {{
          createPost(input: {{ {', '.join(parts)} }}) {{
            __typename
            ... on PostActionSuccess {{ post {{ id status dueAt channelId isCustomScheduled }} }}
            ... on MutationError {{ message }}
          }}
        }}"""
        res = self.gql(q, op="create_post").get("createPost") or {}
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
        return self.gql(q, op="get_post").get("post")

    def posts(self, org_id, channel_id, status="sent", first=20, extra_fields=(),
              after=None, due_start=None, due_end=None):
        """One page of posts with metrics. `status` may be a single value or
        a list; `due_start`/`due_end` (ISO 8601) bound dueAt; `after` is the
        cursor from a previous page. Returns (nodes, page_info).

        This is THE quota lever: one call returns metrics for up to `first`
        posts, so the daily sync costs one or two requests however many
        posts exist, instead of one per post."""
        statuses = [status] if isinstance(status, str) else list(status)
        extra = " ".join(extra_fields)
        filt = [f"status: [{', '.join(statuses)}]", f"channelIds: [{_lit(channel_id)}]"]
        if due_start or due_end:
            parts = []
            if due_start:
                parts.append(f"start: {_lit(due_start)}")
            if due_end:
                parts.append(f"end: {_lit(due_end)}")
            filt.append(f"dueAt: {{ {', '.join(parts)} }}")
        after_arg = f", after: {_lit(after)}" if after else ""
        q = f"""query {{
          posts(first: {int(first)}{after_arg}, input: {{
            organizationId: {_lit(org_id)},
            filter: {{ {', '.join(filt)} }}
          }}) {{
            edges {{ node {{ id text dueAt channelId {extra}
                             metrics {{ type name value unit }} metricsUpdatedAt }} }}
            pageInfo {{ endCursor hasNextPage }}
          }}
        }}"""
        data = self.gql(q, op="posts")
        conn = data.get("posts") or {}
        return ([e["node"] for e in conn.get("edges") or []],
                conn.get("pageInfo") or {})

    def posts_all(self, org_id, channel_id, status="sent", extra_fields=(),
                  due_start=None, page_size=50, max_pages=3):
        """Follow pagination up to max_pages. Each page is one request."""
        out, after = [], None
        for _ in range(max_pages):
            nodes, info = self.posts(org_id, channel_id, status, page_size, extra_fields,
                                     after=after, due_start=due_start)
            out.extend(nodes)
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        return out


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
