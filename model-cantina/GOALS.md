# The Model Cantina — Goals & Decision Log

## Purpose

Joe wants a running, continuously-updated inventory of AI models — frontier
and local/open-weight — broken out by use case, so he's not manually
checking a dozen leaderboard sites to know what's new. Same "watch a
fast-moving space, surface what changed" pattern as Content Radar and Rebel
Intel, applied to models instead of news.

## Goals

**Near-term:** poll every configured source daily, keep a model registry
with score history per category, surface "what's new today" on a dashboard,
let Joe log manual observations for categories with no public benchmark.

**Medium-term:** widen source coverage as new leaderboards appear; harden
the Tier B scrapers as they inevitably break (they're coupled to page/bundle
internals, not stable APIs — see D2).

**Long-term:** cross-reference against the "AI Benchtests" Lumo project's
findings where categories overlap (Software Engineering & Architecture,
Privacy & Trust-Boundary Enforcement) — not built yet, no automated link
between the two projects as of 2026-08-05.

## Design Decisions

**D1 — Every category gets a source, even an imperfect one, labeled
honestly.** Of the 9 categories Joe named, 3 (Software Architecture,
Classification/Routing/Triage, Chunking/Index Enrichment) have no public
benchmark that measures them directly. Rather than leaving those categories
empty, each got the closest available proxy (SWE-bench/Aider for
architecture, Vals.ai's LegalBench for classification, MTEB for chunking)
— explicitly marked as a proxy in `config.yaml`'s `proxy_for` list and shown
with a "(proxy)" badge on the category page. Failure mode if violated:
presenting a proxy score as if it were a direct measurement misleads Joe
into trusting a number that isn't answering the question he's actually
asking.

**D2 — Tier A (structured API/JSON) vs Tier B (scraped) is a first-class
distinction, not just documentation.** `config.yaml` tags every source's
tier, the health page displays it, and `record_poll_failure` /
`get_sources_health` treat every source independently so one broken scraper
never blocks the others. Several Tier B sources (Arena, Aider, IDP
Leaderboard, Vals.ai) work by parsing page-internal formats (JS bundle
chunks, Astro island props, embedded JSON-LD) that are NOT documented APIs
— they will break when those sites redeploy with a different internal
shape. Failure mode if violated: silently trusting stale/wrong data because
nothing flagged the scraper broke. Each Tier B module raises a clear
exception on structural mismatch specifically so this surfaces on the
health page instead of failing silently.

**D3 — Higher score always means better, app-wide, with no exceptions.**
Discovered this the hard way: AgentDojo's "Targeted ASR" (attack success
rate) has *lower* as safer, and the first version of `safety_benchmarks.py`
stored it raw — which would have ranked the least-safe model as #1 on the
Privacy & Trust-Boundary Enforcement leaderboard, since `get_category_leaderboard`
sorts every category `DESC` with no per-source direction logic. Fixed by
inverting at ingestion (`score = 100 - ASR`, renamed to
`agentdojo_targeted_defense_rate`) rather than adding directional
special-casing to storage/templates. Rule for any future source: if the
metric's natural direction is "lower is better," invert it at the
`normalize()` step so the stored score always means "higher is better."

**D4 — Score types are never normalized to one scale.** Elo (unbounded,
relative) and a 0-100 pass rate are not comparable. `scores.score_type` is
always kept alongside `score` so mixed-source category leaderboards show
what's actually being compared, rather than pretending a single sortable
number is an apples-to-apples ranking. This is a known limitation of mixed
Tier A/B category views, not a bug — the "Type" column is how it's exposed
rather than hidden.

**D5 — Manual notes use the same 5-tier rating scale as the "AI Benchtests"
Lumo project** (Recommended / Recommended with safeguards / Useful as a
secondary tool / Experimental only / Not recommended), not a generic
good/bad/mixed. Keeps Joe's rating language consistent across both tools.

**D6 — LOG EVERYTHING.** `event_log` records every poll start/completion/
failure, every newly discovered model, every score recorded, every manual
note added — same directive as Content Radar and Rebel Intel.

**D7 — Two deployments, one codebase, backend picked by environment.**
2026-08-05: Joe wanted a public URL (Thornwick is LAN-only) — added a
Vercel deployment on top of Thornwick, not instead of it. Rather than
forking the code, `db.py` picks SQLite (Thornwick, local dev) or Postgres
(Vercel, via Neon — `DATABASE_URL` env var present) at connect time;
`storage.py`'s business logic is unchanged either way. Both deployments
poll independently against their own database, so their data isn't
identical — acceptable given the goal was a public mirror, not a shared
source of truth (see OQ5 below on future sync).

**D8 — Batch DB writes; the network round-trip is the real cost, not the
fetch.** Measured against real Neon Postgres: the original one-row-at-a-time
`SELECT`-then-`INSERT` loop took 68s for a full poll (~1,300 rows) — over
Vercel Hobby's 60s function ceiling, even with concurrent *fetching*
already in place (`poller.py`). Local SQLite never surfaced this because a
local file has no meaningful round-trip cost; a remote DB does, and it
dominates once you're doing hundreds of them sequentially. Fixed with one
bulk `SELECT ... WHERE name IN (...)` to resolve already-known models plus
one `executemany()` for score inserts — cut it to ~15s warm, ~48s
cold-start (all-new records, only happens once). Lesson for any future
DB-write code path: batch before you optimize anything else, once a remote
DB is in the picture.

**D9 — Vercel's Flask auto-detection owns routing; don't fight it.** First
attempt used an explicit `vercel.json` rewrite (`/(.*) -> /api/index`) to
send every path through one Flask entrypoint — this broke every route
(everything 404'd), because Vercel's "backend framework project" detection
(it recognized Flask via `api/index.py` exposing `app`) rewrites the
*actual* WSGI path to the rewrite destination, not the original request
path. Removing the custom rewrite entirely and letting Vercel's zero-config
Flask detection handle routing fixed it immediately. Lesson: check for a
framework preset before hand-rolling routing config.

## Open Questions

| # | Question | Resolution trigger |
|---|---|---|
| OQ1 | Should Model Cantina cross-reference the Lumo benchmark's findings for overlapping categories (Software Architecture, Privacy/Trust)? | Revisit once both projects have enough data to make a link useful — no integration exists yet. |
| OQ2 | Artificial Analysis intermittently times out (CDN read-timeout, not a structural break) — worth a retry-with-backoff instead of a single-attempt failure? | Revisit if the health page shows repeated failures over multiple days, not just an occasional one. |
| OQ3 | The Tier B scrapers (Arena, Aider, IDP Leaderboard, Vals.ai) are coupled to page-internal formats that will eventually break. Worth budgeting for headless-browser fallback (Playwright) for the hardest cases? | Revisit when a source's health status goes to `error` and doesn't self-resolve — diagnose whether it's a transient network issue or a real structural break first. |
| OQ4 | Should there be a daily email digest, like Rebel Intel's `notify.py`? | Joe explicitly deferred this for v1 (dashboard-only) on 2026-08-05 — revisit if checking the dashboard daily becomes tedious. |
| OQ5 | Thornwick and Vercel poll independently into separate databases, so their model counts/scores will drift apart over time. Worth unifying (e.g. Thornwick becomes the only poller, Vercel just reads Thornwick's data some way) instead of two independent trackers? | Revisit if the divergence actually confuses Joe in practice — not a problem on day one when both are freshly seeded. |
| OQ6 | Cold-start poll against Postgres (~48s) has less headroom under Vercel's 60s ceiling than steady-state (~15s). Only matters once (first-ever poll) at current data volume — would it matter again if many more sources get added later? | Revisit only if a future source addition measurably pushes cold-start close to 60s again. |

## Roadmap

- [x] Data model + storage.py + radar.py CLI skeleton
- [x] Tier A sources (SWE-bench, MTEB, Hugging Face Hub)
- [x] Dashboard v1 (home, models, model detail, category, health)
- [x] Tier B sources (Arena, LiveCodeBench, Aider, Ollama library,
      Artificial Analysis, OCR benchmarks, safety benchmarks, Vals.ai)
- [x] Manual-notes feature
- [x] Deployed to Thornwick (systemd --user + per-user cron, daily 6am poll)
- [x] Column sorting, search, and pagination across all dashboard tables
- [x] Postgres backend (db.py) alongside SQLite, selected by DATABASE_URL
- [x] Deployed to Vercel (model-cantina.vercel.app), Postgres via Neon,
      CRON_SECRET-protected /cron/poll, daily 11:00 UTC
- [x] Thornwick migrated from scp deploys to git pull (sparse-checkout of
      model-cantina/ from the linkedin-chatgpt repo) — one source of truth,
      daily cron does `git pull --ff-only` before polling
- [ ] OQ1–OQ6 above, as they come up
