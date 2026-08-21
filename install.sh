#!/usr/bin/env bash
# VPS installer. Safe to re-run — every step checks before it acts.
#
#   sudo bash install.sh
#
# Sets up everything that does not need a human: packages, virtualenv, the
# clipper service user, /etc/clipper for secrets, systemd units, cron.
# It deliberately does NOT write any credential — see SETUP.md for the parts
# only a person can do (creating apps, granting OAuth, exporting cookies).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/clipper}"
ETC_DIR="${ETC_DIR:-/etc/clipper}"
SVC_USER="${SVC_USER:-clipper}"
REPO="${REPO:-https://github.com/ekfal/clipper.git}"
BRANCH="${BRANCH:-main}"

say() { printf '\n=== %s\n' "$1"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash install.sh"; exit 1; }

say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ffmpeg fonts-dejavu \
    fonts-noto-color-emoji nginx cron curl

say "service user $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

say "repo at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only || echo "pull skipped (local changes?)"
else
    git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/out" "$APP_DIR/media"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

say "python environment"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/playwright" install --with-deps chromium \
    || "$APP_DIR/.venv/bin/playwright" install --with-deps chromium
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/.venv"

# Secrets live outside the repo so an agent working in a git worktree cannot
# read them, and so a stray `git add -A` can never commit one.
say "secrets directory $ETC_DIR"
mkdir -p "$ETC_DIR/tokens"
if [ ! -f "$ETC_DIR/env" ]; then
    cat > "$ETC_DIR/env" <<'ENVEOF'
# Fill these in. See SETUP.md for where each value comes from.
CLIPPER_TOKEN_DIR=/etc/clipper/tokens
CLIPPER_CLIENT_SECRETS=/etc/clipper/client_secrets.json
CLIPPO_SESSION=/etc/clipper/clippo_session.json
CLIPPER_YT_COOKIES=/etc/clipper/cookies.txt

NINEROUTER_BASE_URL=
NINEROUTER_API_KEY=
CLIPPER_DISCORD_WEBHOOK=

TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
INSTAGRAM_APP_ID=
INSTAGRAM_APP_SECRET=

CLIPPER_CONNECT_ORIGIN=https://CHANGEME
CLIPPER_CONNECT_KEY=REPLACE_ME
CLIPPER_PUBLIC_BASE=https://CHANGEME/clips
CLIPPER_BRAND=Clipper
CLIPPER_CONTACT=
ENVEOF
    # A predictable connect key is the same as no key at all.
    sed -i "s|CLIPPER_CONNECT_KEY=REPLACE_ME|CLIPPER_CONNECT_KEY=$(openssl rand -hex 24)|" \
        "$ETC_DIR/env"
    echo "created $ETC_DIR/env — fill in the blanks"
else
    echo "$ETC_DIR/env exists, left alone"
fi
chown -R "$SVC_USER:$SVC_USER" "$ETC_DIR"
chmod 700 "$ETC_DIR" "$ETC_DIR/tokens"
chmod 600 "$ETC_DIR/env"

say "systemd units"
write_unit() {
    local name="$1" desc="$2" cmd="$3"
    cat > "/etc/systemd/system/$name" <<UNITEOF
[Unit]
Description=$desc
After=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ETC_DIR/env
ExecStart=$cmd
Restart=always
RestartSec=5
# The service reads its secrets and writes its own output; nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNITEOF
}
write_unit clipper-connect.service "Clipper connect + OAuth" \
    "$APP_DIR/.venv/bin/python $APP_DIR/connect.py"
write_unit clipper-dashboard.service "Clipper accounts dashboard" \
    "$APP_DIR/.venv/bin/python $APP_DIR/dashboard.py"
systemctl daemon-reload
systemctl enable --now clipper-connect.service clipper-dashboard.service

say "hourly run"
chmod +x "$APP_DIR/run.sh"
cat > /etc/cron.d/clipper <<CRONEOF
# Clipper hourly pipeline. run.sh takes a flock, so an overrunning run is
# skipped rather than doubled up.
SHELL=/bin/bash
0 * * * * $SVC_USER . $ETC_DIR/env; cd $APP_DIR && ./run.sh >> $APP_DIR/clipper.log 2>&1
CRONEOF
chmod 644 /etc/cron.d/clipper

say "nginx"
if [ ! -f /etc/nginx/sites-available/clipper ]; then
    cat > /etc/nginx/sites-available/clipper <<'NGINXEOF'
# Replace CHANGEME with your domain, then run certbot --nginx -d your.domain
server {
    listen 80;
    server_name CHANGEME;

    # Meta fetches Reels from a public URL, so rendered clips must be readable.
    location /clips/ {
        alias /opt/clipper/out/;
        autoindex off;
    }

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINXEOF
    ln -sf /etc/nginx/sites-available/clipper /etc/nginx/sites-enabled/clipper
    echo "wrote /etc/nginx/sites-available/clipper — set server_name, then run certbot"
else
    echo "nginx site exists, left alone"
fi
nginx -t && systemctl reload nginx || echo "nginx config needs attention"

say "done"
cat <<DONEEOF
Next, by hand (see SETUP.md):
  1. edit $ETC_DIR/env
  2. set server_name in /etc/nginx/sites-available/clipper, then:
     certbot --nginx -d your.domain
  3. put client_secrets.json, clippo_session.json and cookies.txt in $ETC_DIR
  4. connect key:  grep CONNECT_KEY $ETC_DIR/env
  5. verify:       sudo -u $SVC_USER $APP_DIR/.venv/bin/python $APP_DIR/preflight.py
DONEEOF
