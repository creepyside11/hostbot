#!/bin/sh
set -eu
# Bothost mounts /app at runtime, so ensure the persistent subdirectory is writable.
mkdir -p "${DATA_DIR:-/app/data/emerald}"
chown emerald:emerald "${DATA_DIR:-/app/data/emerald}"
exec gosu emerald /usr/bin/tini -g -- /opt/emerald/venv/bin/python /opt/emerald/launcher.py
