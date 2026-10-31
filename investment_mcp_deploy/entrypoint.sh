#!/bin/sh
set -eu
# Fly volumes mount as root. Change only the mount root owner, never recurse
# through an existing dataset. The application always runs as uid/gid 10001.
if [ "$(id -u)" = 0 ]; then
    chown 10001:10001 "${INVESTMENT_DATA_DIR:-/data}"
    exec python -c 'import os,sys; os.setgroups([]); os.setgid(10001); os.setuid(10001); os.execvp(sys.argv[1],sys.argv[1:])' "$@"
fi
exec "$@"
