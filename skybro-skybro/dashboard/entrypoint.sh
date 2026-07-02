#!/bin/sh
set -e
mkdir -p /data
chown -R skybro:skybro /data
exec su -p -s /bin/sh skybro -c "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 30 app:app"
