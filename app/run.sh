#!/usr/bin/env bash
# Launch the standalone TOM Driver App locally.
#   ./run.sh                  → http://127.0.0.1:5179
#   GOOGLE_MAPS_BROWSER_KEY=... GOOGLE_MAPS_SERVER_KEY=... ./run.sh   (enable Maps)
set -e
cd "$(dirname "$0")"
export DRIVER_APP_PORT="${DRIVER_APP_PORT:-5179}"
# Local dev launcher: declare the environment and opt in to the demo seed
# explicitly (production deployments must set DRIVER_APP_ENV=production).
export DRIVER_APP_ENV="${DRIVER_APP_ENV:-development}"
export DRIVER_APP_SEED_DEMO="${DRIVER_APP_SEED_DEMO:-1}"
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")
echo "TOM Driver App"
echo "  Local  → http://127.0.0.1:${DRIVER_APP_PORT}"
echo "  Network→ http://${LAN_IP}:${DRIVER_APP_PORT}   (demo login: DRV001 / test1234)"
exec python3 serve.py
