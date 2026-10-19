#!/bin/sh
# Optional install: run CouncilLogic as its own OS user (Linux or macOS).
#
#   sudo ./install.sh --wheel /path/model_council-*.whl --agent-user AGENTUSER --apply
#
# Without --apply it prints every command and changes nothing (the default).
# What it sets up:
#   user/group  councillogic (system account, no login shell)
#   group       councillogic-agents (agent accounts you add to it)
#   /opt/councillogic/venv            root-owned venv with the wheel (0755)
#   /var/lib/councillogic             service home, 0700 councillogic
#   /var/lib/councillogic/data        data dir: SQLite, approval.key (0700)
#   /var/lib/councillogic/keys        provider keys, one file per key (0700/0600)
#   /usr/local/libexec/councillogic/  agent-entry, human-entry, read-key (root 0755)
#   /usr/local/bin/council-agent, council-mcp-agent   agent wrappers (root 0755)
#   /etc/sudoers.d/councillogic       narrow NOPASSWD rule for the agents group
set -eu
APPLY=0
WHEEL=""
AGENT_USER=""
PYTHON="${PYTHON:-python3}"
HERE=$(cd "$(dirname "$0")" && pwd)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --wheel) WHEEL="$2"; shift ;;
    --agent-user) AGENT_USER="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
  shift
done
[ -n "$WHEEL" ] || { echo "--wheel is required" >&2; exit 64; }
OS=$(uname -s)

run() {
  if [ "$APPLY" -eq 1 ]; then
    echo "+ $*"
    "$@"
  else
    echo "would run: $*"
  fi
}

if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "--apply must run as root (sudo)" >&2
  exit 77
fi

case "$OS" in
  Linux)
    run groupadd --system councillogic-agents
    run useradd --system --user-group --home-dir /var/lib/councillogic \
        --shell /usr/sbin/nologin councillogic
    [ -z "$AGENT_USER" ] || run usermod -aG councillogic-agents "$AGENT_USER"
    ;;
  Darwin)
    # Pick a free hidden UID/GID below 500.
    ID=$(dscl . -list /Users UniqueID | awk '{print $2}' | sort -n | \
         awk 'BEGIN{n=400} $1==n{n++} END{print n}')
    run dscl . -create /Groups/councillogic PrimaryGroupID "$ID"
    run dscl . -create /Groups/councillogic-agents PrimaryGroupID "$((ID + 1))"
    run dscl . -create /Users/councillogic UniqueID "$ID"
    run dscl . -create /Users/councillogic PrimaryGroupID "$ID"
    run dscl . -create /Users/councillogic UserShell /usr/bin/false
    run dscl . -create /Users/councillogic NFSHomeDirectory /var/lib/councillogic
    run dscl . -create /Users/councillogic IsHidden 1
    [ -z "$AGENT_USER" ] || run dseditgroup -o edit -a "$AGENT_USER" -t user councillogic-agents
    ;;
  *) echo "unsupported OS: $OS" >&2; exit 69 ;;
esac

run install -d -o root -m 0755 /opt/councillogic /usr/local/libexec/councillogic
run "$PYTHON" -m venv /opt/councillogic/venv
run /opt/councillogic/venv/bin/pip install --no-deps "$WHEEL"
run install -d -o councillogic -g councillogic -m 0700 \
    /var/lib/councillogic /var/lib/councillogic/data /var/lib/councillogic/keys
for f in agent-entry human-entry read-key; do
  run install -o root -m 0755 "$HERE/$f" "/usr/local/libexec/councillogic/$f"
done
for f in council-agent council-mcp-agent; do
  run install -o root -m 0755 "$HERE/$f" "/usr/local/bin/$f"
done
run visudo -cf "$HERE/sudoers.councillogic"
run install -o root -m 0440 "$HERE/sudoers.councillogic" /etc/sudoers.d/councillogic
echo "Next: add keys with ./add-key.sh NAME (as root), then test: echo '{\"action\":\"capabilities\"}' | council-agent"
