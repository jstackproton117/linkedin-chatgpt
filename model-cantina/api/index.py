"""Vercel Python entrypoint — exposes the existing Flask app (app.py,
unchanged, shared with the Thornwick deployment) as a WSGI handler.

Vercel's Python runtime auto-detects a module-level `app` variable in an
api/*.py file and serves it as a WSGI app. vercel.json rewrites every
non-/api/ path here so Flask's own routing (/, /models, /category/<key>,
etc.) handles everything, same as it does on Thornwick.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402
