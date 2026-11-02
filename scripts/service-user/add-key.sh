#!/bin/sh
# Store one provider key for the councillogic user, read from the terminal
# without echo (never from argv, so it is not in shell history or ps):
#   sudo ./add-key.sh OPENAI_API_KEY
set -eu
name="${1:-}"
case "$name" in ''|*[!A-Z0-9_]*) echo "usage: add-key.sh KEY_NAME" >&2; exit 64 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 77; }
printf 'Paste %s (input hidden): ' "$name" >&2
stty -echo; IFS= read -r value; stty echo; echo >&2
[ -n "$value" ] || { echo "empty key" >&2; exit 65; }
umask 077
tmp=$(mktemp /var/lib/councillogic/keys/.tmp.XXXXXX)
printf '%s' "$value" > "$tmp"
chown councillogic:councillogic "$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "/var/lib/councillogic/keys/$name"
echo "stored $name; rotate by running this again" >&2
