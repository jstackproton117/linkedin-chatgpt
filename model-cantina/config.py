"""Config/secrets loading for The Model Cantina."""

import json
import os
from pathlib import Path

import yaml

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.yaml"
SECRETS_PATH = BASE / "secrets.json"

# Secret keys this app uses, as they appear in secrets.json — the
# corresponding environment variable is just the uppercased version
# (hf_token -> HF_TOKEN).
_KNOWN_SECRET_KEYS = ("hf_token",)

_config = None
_secrets = None


def load_config():
    global _config
    if _config is None:
        _config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return _config


def load_secrets():
    """secrets.json (Thornwick/local dev) or environment variables (Vercel,
    which has no local disk to keep a secrets file on) — env vars win if
    both are present. On Vercel there's no secrets.json at all, so the
    FileNotFoundError path is the normal case there, not an error."""
    global _secrets
    if _secrets is None:
        try:
            _secrets = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _secrets = {}
        for key in _KNOWN_SECRET_KEYS:
            env_val = os.environ.get(key.upper())
            if env_val:
                _secrets[key] = env_val
    return _secrets


def source_keys():
    return list(load_config()["sources"].keys())


def source_config(source_key):
    return load_config()["sources"][source_key]


def category_name(category_key):
    return load_config()["categories"].get(category_key, {}).get("name", category_key)


def _prettify_score_type(score_type):
    return (score_type or "unknown").replace("_", " ").strip().capitalize()


def score_type_label(score_type):
    entry = load_config().get("score_types", {}).get(score_type)
    return entry["label"] if entry else _prettify_score_type(score_type)


def score_type_description(score_type):
    entry = load_config().get("score_types", {}).get(score_type)
    return entry["description"] if entry else None
