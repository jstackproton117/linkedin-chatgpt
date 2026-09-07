#!/usr/bin/env bash
# Rebel Intel — reach the dashboard from your iPhone anywhere, privately.
#
# Installs Tailscale (a private mesh VPN between your own devices), signs
# this machine in, and fronts the dashboard with HTTPS on the tailnet.
# Nothing is exposed to the public internet and no router changes are made.
#
# Run:   sudo bash /home/joe/rebel-intel/setup_tailscale.sh
# Safe to re-run; every step checks state first.

set -uo pipefail
PORT=5000
OWNER=joe
APP=/home/joe/rebel-intel

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash $0"; exit 1
fi
step() { echo; echo "══ $* ══"; }

step "1/4  Install Tailscale"
if command -v tailscale >/dev/null 2>&1; then
  echo "already installed: $(tailscale version | head -1)"
else
  # Official installer: adds Tailscale's signed apt repo and installs the
  # package. Inspect it first if you like: https://tailscale.com/install.sh
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled >/dev/null 2>&1 || true

step "2/4  Sign in"
if tailscale status >/dev/null 2>&1 && tailscale ip -4 >/dev/null 2>&1; then
  echo "already signed in as $(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("Self",{}).get("DNSName","?").rstrip("."))')"
else
  echo "A login URL will print below. Open it on this PC or your phone and"
  echo "sign in (Google/Apple/GitHub/Microsoft all work). This waits for you."
  echo
  # --operator lets the joe account run 'tailscale serve' without sudo later.
  tailscale up --operator="$OWNER"
fi

step "3/4  Where the dashboard lives on your tailnet"
IP4=$(tailscale ip -4 2>/dev/null || echo "?")
FQDN=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("Self",{}).get("DNSName","").rstrip("."))')
SHORT=${FQDN%%.*}
echo "tailnet IP : $IP4"
echo "MagicDNS   : $FQDN"
echo "plain HTTP : http://$SHORT:$PORT   (works as soon as the iPhone app is signed in)"

step "4/4  HTTPS (optional but recommended)"
HTTPS_URL=""
if tailscale serve --bg "$PORT" >/dev/null 2>&1; then
  HTTPS_URL="https://$FQDN"
  echo "enabled: $HTTPS_URL"
  tailscale serve status 2>/dev/null || true
else
  echo "Not enabled yet. HTTPS needs one switch flipped in the admin console:"
  echo "  https://login.tailscale.com/admin/dns  ->  'Enable HTTPS'"
  echo "then re-run this script (or just:  tailscale serve --bg $PORT)."
  echo "HTTPS makes Safari treat it as a real site: no 'not secure' banner,"
  echo "'Add to Home Screen' works as an app, and the copy buttons use the"
  echo "native clipboard."
fi

# Point the daily reminder emails at a URL that works off the LAN.
DASH="${HTTPS_URL:-http://$SHORT:$PORT}"
if [ -n "$SHORT" ] && [ -f "$APP/notify_config.json" ]; then
  cp -p "$APP/notify_config.json" "$APP/notify_config.json.bak-$(date +%Y%m%d-%H%M%S)"
  python3 - "$APP/notify_config.json" "$DASH" <<'PY'
import json, sys
p, url = sys.argv[1], sys.argv[2]
d = json.load(open(p)); old = d.get("dashboard_url")
d["dashboard_url"] = url
json.dump(d, open(p, "w"), indent=2)
print(f"notify emails now link to {url}  (was {old})")
PY
  chown "$OWNER:$OWNER" "$APP/notify_config.json"; chmod 600 "$APP/notify_config.json"
fi

echo
echo "══ On the iPhone ══"
echo "1. App Store -> 'Tailscale' -> sign in with the SAME account."
echo "2. Open Safari to: $DASH"
echo "3. Share -> 'Add to Home Screen' for a one-tap app icon."
echo
echo "Only devices signed into your tailnet can reach it. Nothing is public."
