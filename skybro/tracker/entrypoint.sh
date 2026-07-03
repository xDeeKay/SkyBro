#!/bin/sh
set -e
mkdir -p /data
chown -R skybro:skybro /data
exec su -p -s /bin/sh skybro -c "$*"
