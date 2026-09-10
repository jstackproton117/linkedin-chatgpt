# Moved — Rebel Intel now lives in its own repository

As of 2026-09-09, the Rebel Intel pipeline is no longer tracked here.

**Repository:** https://github.com/jstackproton117/RebelNews (private)
**Live deployment:** `~/rebel-intel` on RedRose (`ssh redrose`),
`rebel-intel.service` under `systemctl --user`, dashboard on port 5000.

## Why it moved

It was a project in its own right sharing a repo with the Model Cantina, and
this folder tracked 154 files under `pipeline/data/` — articles, drafts,
post log, embeddings. That is runtime state, not source: it churns on every
daily run, bloats diffs, and made the repo history mostly noise. RebelNews
tracks code only, with `data/`, `logs/` and `backups/` ignored.

## What is still here

Nothing but this note. The files remain on disk in this folder, untracked, and
the full history is preserved — everything up to the commit that removed them
is still in this repository's log:

```sh
git log --  pipeline/
git show <commit>:pipeline/app.py     # any file, any point in its history
```

Nothing was deleted. If you need the old tracked copy of a file, it is in the
history above; if you need the current one, it is in RebelNews or on RedRose.

## What did NOT move

`model-cantina/` stays in this repository. Thornwick still deploys it by
`git pull --ff-only` on its daily 06:00 cron, unchanged — see
`model-cantina/MOVED_TO_THORNWICK.md`.

## Note on the previous marker

The `MOVED_TO_NUC.md` that used to sit here described this folder as "a frozen
backup only." That had stopped being true: the folder was actively developed
and committed right up to the move, while the live copy on RedRose was edited
separately. Keeping one authoritative repo per project is the point of this
change.
