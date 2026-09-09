"""
Rebel Intel — draft generation engines.

Runs a prompt file (data/drafts/*_prompt.txt) through a model and returns
the response, ready to be written into the matching *_draft.md exactly as
if it had been pasted in by hand.

Two engines:

  local   — any Ollama endpoint (RedRose's 7B today; point it at the
            dual-5090 box for a 30B model). No cost, slow-ish.
  claude  — the Anthropic API via the official `anthropic` SDK. The key
            lives in anthropic_config.json (gitignored, chmod 600) and is
            never returned to the browser. Every call is logged with token
            counts and an estimated cost in data/drafting_log.jsonl.

Generation runs in a background thread; state lives in data/drafting_jobs/
so any gunicorn worker can answer a status poll.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
DATA = BASE / "data"
ANTHROPIC_CONFIG_PATH = BASE / "anthropic_config.json"
DRAFTING_LOG_PATH = DATA / "drafting_log.jsonl"
JOBS_DIR = DATA / "drafting_jobs"

# Per-million-token prices (USD) used for the cost estimate that is logged
# with every Claude call. Cache reads are billed at 10% of input; cache
# writes at 125%. Update here if pricing changes — nothing else depends on it.
CLAUDE_MODELS = {
    "claude-opus-5":    {"label": "Claude Opus 5",   "input": 5.00, "output": 25.00, "note": "best quality (default)"},
    "claude-sonnet-5":  {"label": "Claude Sonnet 5", "input": 2.00, "output": 10.00, "note": "cheaper, still strong"},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "input": 1.00, "output": 5.00, "note": "fastest, cheapest"},
}
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

DEFAULT_LOCAL_URL = "http://127.0.0.1:11434"
DEFAULT_LOCAL_MODEL = "qwen2.5:7b"
DEFAULT_LOCAL_TIMEOUT = 900   # a 30B model on a busy box can take minutes


class DraftingError(Exception):
    pass


# ── Settings ─────────────────────────────────────────────────────────────

def drafting_settings(settings):
    """The `drafting` block of settings.json with defaults filled in.
    Falls back to the classifier's local_model URL so a fresh install
    drafts on the same Ollama it already uses."""
    d = dict(settings.get("drafting") or {})
    lm = settings.get("local_model") or {}
    fallback_url = re.sub(r"/api/.*$", "", lm.get("url") or DEFAULT_LOCAL_URL)
    local = dict(d.get("local") or {})
    local.setdefault("url", fallback_url or DEFAULT_LOCAL_URL)
    local.setdefault("model", lm.get("model") or DEFAULT_LOCAL_MODEL)
    local.setdefault("timeout", DEFAULT_LOCAL_TIMEOUT)
    claude = dict(d.get("claude") or {})
    claude.setdefault("model", DEFAULT_CLAUDE_MODEL)
    claude.setdefault("max_tokens", 6000)
    claude.setdefault("effort", "medium")
    return {
        "default_engine": d.get("default_engine") or "local",
        "local": local,
        "claude": claude,
    }


def _base_url(url):
    return (url or "").strip().rstrip("/")


# ── Anthropic key storage ────────────────────────────────────────────────

def load_anthropic_config():
    if not ANTHROPIC_CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(ANTHROPIC_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise DraftingError(f"anthropic_config.json is unreadable: {e}")
    key = (cfg.get("api_key") or "").strip()
    if not key or key.startswith("PASTE_"):
        return {}
    return cfg


def save_anthropic_config(cfg):
    ANTHROPIC_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        ANTHROPIC_CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def anthropic_status(settings=None):
    """What Settings shows about Claude. Never includes the key."""
    try:
        cfg = load_anthropic_config()
    except DraftingError as e:
        return {"connected": False, "error": str(e)}
    key = cfg.get("api_key", "")
    return {
        "connected": bool(key),
        "key_hint": ("…" + key[-4:]) if len(key) >= 4 else "",
        "validated_at": cfg.get("validated_at"),
        "models_seen": cfg.get("models_seen") or [],
        "usage": usage_summary(),
    }


def _client(cfg=None):
    import anthropic
    cfg = cfg or load_anthropic_config()
    if not cfg:
        raise DraftingError("Claude is not connected. Add an API key on the Settings page.")
    return anthropic.Anthropic(api_key=cfg["api_key"], max_retries=2)


def validate_anthropic_key(key):
    """One cheap request (GET /v1/models). Returns the model ids the key can
    see. Raises DraftingError with a readable message on failure."""
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=20.0)
    try:
        page = client.models.list(limit=50)
    except anthropic.AuthenticationError:
        raise DraftingError("Anthropic rejected that key (401). Check it was copied in full.")
    except anthropic.PermissionDeniedError:
        raise DraftingError("Key accepted but it lacks permission to list models (403).")
    except anthropic.APIConnectionError as e:
        raise DraftingError(f"Could not reach api.anthropic.com: {e}")
    except anthropic.APIStatusError as e:
        raise DraftingError(f"Anthropic returned {e.status_code}: {e.message}")
    return [m.id for m in page.data]


# ── Ollama ───────────────────────────────────────────────────────────────

def list_local_models(url, timeout=10):
    base = _base_url(url)
    try:
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except requests.RequestException as e:
        raise DraftingError(f"Could not reach Ollama at {base}: {e}")
    except (ValueError, KeyError) as e:
        raise DraftingError(f"Unexpected reply from {base}/api/tags: {e}")


def draft_local(prompt, url, model, timeout=DEFAULT_LOCAL_TIMEOUT):
    base = _base_url(url)
    t0 = time.time()
    try:
        r = requests.post(
            f"{base}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.7, "num_ctx": 8192},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except requests.Timeout:
        raise DraftingError(f"{model} at {base} did not answer within {timeout}s.")
    except requests.RequestException as e:
        raise DraftingError(f"Ollama at {base} failed: {e}")
    text = (data.get("message") or {}).get("content", "").strip()
    if not text:
        raise DraftingError(f"{model} returned an empty response.")
    return {
        "text": clean_response(text),
        "engine": "local",
        "model": model,
        "endpoint": base,
        "input_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
        "cost_usd": 0.0,
        "elapsed_s": round(time.time() - t0, 1),
    }


# ── Claude ───────────────────────────────────────────────────────────────

def estimate_cost(model, usage):
    p = CLAUDE_MODELS.get(model)
    if not p:
        return None
    inp = (usage.get("input_tokens") or 0) * p["input"]
    cr = (usage.get("cache_read_input_tokens") or 0) * p["input"] * 0.10
    cw = (usage.get("cache_creation_input_tokens") or 0) * p["input"] * 1.25
    out = (usage.get("output_tokens") or 0) * p["output"]
    return round((inp + cr + cw + out) / 1_000_000, 5)


def draft_claude(prompt, model=DEFAULT_CLAUDE_MODEL, max_tokens=6000, effort="medium", cfg=None):
    import anthropic
    client = _client(cfg)
    t0 = time.time()
    kwargs = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
    }
    # Adaptive thinking on the 4.6+ / 5 models; Haiku 4.5 rejects it.
    if not model.startswith("claude-haiku"):
        kwargs["thinking"] = {"type": "adaptive"}
        if effort in ("low", "medium", "high"):
            kwargs["output_config"] = {"effort": effort}
    try:
        # Streaming so a long generation never trips a request timeout.
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise DraftingError("Anthropic rejected the stored key (401). Reconnect on the Settings page.")
    except anthropic.RateLimitError:
        raise DraftingError("Anthropic rate limit hit (429). Try again in a minute.")
    except anthropic.APIConnectionError as e:
        raise DraftingError(f"Could not reach api.anthropic.com: {e}")
    except anthropic.APIStatusError as e:
        raise DraftingError(f"Anthropic returned {e.status_code}: {e.message}")

    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()
    usage = {
        "input_tokens": getattr(message.usage, "input_tokens", 0),
        "output_tokens": getattr(message.usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
    }
    if message.stop_reason == "refusal":
        raise DraftingError("Claude declined this prompt (refusal).")
    if not text:
        raise DraftingError(f"Claude returned no text (stop_reason={message.stop_reason}).")
    return {
        "text": clean_response(text),
        "engine": "claude",
        "model": message.model or model,
        "endpoint": "api.anthropic.com",
        "stop_reason": message.stop_reason,
        "request_id": getattr(message, "_request_id", None),
        "cost_usd": estimate_cost(model, usage),
        "elapsed_s": round(time.time() - t0, 1),
        **usage,
    }


# ── Shared ───────────────────────────────────────────────────────────────

_PREAMBLE = re.compile(
    r"^(here (is|are)|sure[,!]|of course[,!]|certainly[,!]|below (is|are)|i('ve| have) (drafted|written))",
    re.IGNORECASE,
)


def clean_response(text):
    """Strip a wrapping code fence and a one-line chat preamble. Everything
    else is kept — the prompt asks for two drafts with headers and character
    counts, and the existing paste flow stores exactly that."""
    t = text.strip()
    fence = re.fullmatch(r"```[a-zA-Z]*\n(.*?)\n```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    lines = t.split("\n")
    if lines and _PREAMBLE.match(lines[0].strip()) and len(lines[0]) < 160:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def generate(prompt, engine, settings, model_override=None):
    ds = drafting_settings(settings)
    if engine == "local":
        lc = ds["local"]
        return draft_local(prompt, lc["url"], model_override or lc["model"], int(lc.get("timeout") or DEFAULT_LOCAL_TIMEOUT))
    if engine == "claude":
        cc = ds["claude"]
        model = model_override or cc["model"]
        if model not in CLAUDE_MODELS:
            raise DraftingError(f"Unknown Claude model {model!r}.")
        return draft_claude(prompt, model, cc.get("max_tokens", 6000), cc.get("effort", "medium"))
    raise DraftingError(f"Unknown engine {engine!r}.")


# ── Log ──────────────────────────────────────────────────────────────────

def log_call(record):
    DATA.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    with DRAFTING_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _read_log():
    if not DRAFTING_LOG_PATH.exists():
        return []
    out = []
    for line in DRAFTING_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def usage_summary():
    """Claude spend for Settings: calls and estimated dollars, today and 30 days."""
    now = datetime.now(timezone.utc)
    day = now - timedelta(days=1)
    month = now - timedelta(days=30)
    s = {"calls_24h": 0, "cost_24h": 0.0, "calls_30d": 0, "cost_30d": 0.0,
         "local_calls_30d": 0, "last_call": None, "last_model": None}
    for r in _read_log():
        try:
            ts = datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        if r.get("engine") == "local":
            if ts >= month:
                s["local_calls_30d"] += 1
            continue
        if r.get("engine") != "claude":
            continue
        cost = r.get("cost_usd") or 0.0
        if ts >= month:
            s["calls_30d"] += 1
            s["cost_30d"] += cost
        if ts >= day:
            s["calls_24h"] += 1
            s["cost_24h"] += cost
        s["last_call"] = r["ts"]
        s["last_model"] = r.get("model")
    s["cost_24h"] = round(s["cost_24h"], 4)
    s["cost_30d"] = round(s["cost_30d"], 4)
    return s


# ── Background jobs ──────────────────────────────────────────────────────

def _job_path(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
        raise DraftingError("Bad job id.")
    return JOBS_DIR / f"{job_id}.json"


def _write_job(job_id, state):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _job_path(job_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, _job_path(job_id))


def read_job(job_id):
    p = _job_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def prune_jobs(max_age_hours=24):
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for p in JOBS_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


def active_job_for(filename):
    """A running job for this draft file, if any — stops double-clicks
    from launching two generations."""
    if not JOBS_DIR.exists():
        return None
    for p in JOBS_DIR.glob("*.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("filename") == filename and j.get("status") == "running":
            if time.time() - p.stat().st_mtime < 3600:
                return j
    return None


def start_job(filename, engine, model, prompt, settings, on_success):
    """Kick off generation in a thread. `on_success(job, result)` runs in the
    thread once the model answers and is where the caller writes the draft
    file and post log. Returns the job id immediately."""
    prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    state = {
        "id": job_id, "filename": filename, "engine": engine, "model": model,
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_job(job_id, state)

    def run():
        t0 = time.time()
        try:
            result = generate(prompt, engine, settings, model_override=model)
            on_success(state, result)
            log_call({"filename": filename, "ok": True,
                      **{k: v for k, v in result.items() if k != "text"}})
            state.update({"status": "done", "model": result.get("model"),
                          "elapsed_s": result.get("elapsed_s"),
                          "cost_usd": result.get("cost_usd"),
                          "input_tokens": result.get("input_tokens"),
                          "output_tokens": result.get("output_tokens"),
                          "chars": len(result.get("text") or "")})
        except DraftingError as e:
            log_call({"filename": filename, "engine": engine, "model": model, "ok": False,
                      "error": str(e), "elapsed_s": round(time.time() - t0, 1)})
            state.update({"status": "error", "error": str(e)})
        except Exception as e:  # never leave a job stuck on "running"
            log_call({"filename": filename, "engine": engine, "model": model, "ok": False,
                      "error": f"{type(e).__name__}: {e}", "elapsed_s": round(time.time() - t0, 1)})
            state.update({"status": "error", "error": f"{type(e).__name__}: {e}"})
        state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_job(job_id, state)

    threading.Thread(target=run, name=f"draft-{job_id}", daemon=True).start()
    return job_id
