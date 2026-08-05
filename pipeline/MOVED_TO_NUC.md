# This copy is retired — Rebel Intel now runs on the NUC

As of 2026-08-04, the live Rebel Intel dashboard and notify job run on the
RedRose NUC (192.168.1.233), not here. This OneDrive folder is a frozen
backup only — editing files here has no effect on the running app.

Live location: `~/rebel-intel` on RedRose (`ssh redrose`, or `ssh joe@192.168.1.233`)
- Dashboard: `systemctl --user status rebel-intel.service` — auto-starts on boot via linger, no login needed
- Daily notify email: cron job, `0 8 * * *`, logs to `~/rebel-intel/logs/notify.log`
- Dashboard URL: http://192.168.1.233:5000
- Ollama also runs on this same box at 127.0.0.1:11434 (qwen2.5:7b)

The Windows scheduled task "Rebel Intel Notify" was disabled to avoid duplicate emails.

To make a code change: edit the files on the NUC directly, or edit here and
`scp`/`rsync` over, then `systemctl --user restart rebel-intel.service`.
