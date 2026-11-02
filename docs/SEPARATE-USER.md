# Optional install: CouncilLogic as its own OS user

Use this when agents you don't fully trust will call CouncilLogic. CouncilLogic
then runs as a dedicated `councillogic` account. Its data folder, approval
key and provider keys are readable only by that account. Agents reach it only
through two wrapper commands, and they can't read the database, the approval
key or the keys, or run `approve` or `decide`.

The scripts are in `scripts/service-user/`. They work on Linux and macOS and
need `sudo`. `install.sh` is a **dry run by default**: it prints every command
and changes nothing until you add `--apply`.

## Layout

| Path | Owner / mode | Purpose |
|---|---|---|
| `/opt/councillogic/venv` | root, 0755 | Installed wheel (agents can't modify code) |
| `/var/lib/councillogic` | councillogic, 0700 | Service home |
| `/var/lib/councillogic/data` | councillogic, 0700 | SQLite run store, `approval.key` (0600), worker logs |
| `/var/lib/councillogic/keys/<KEY_NAME>` | councillogic, 0700 / 0600 | One provider key per file |
| `/usr/local/libexec/councillogic/agent-entry` | root, 0755 | Accepts only `agent` or `mcp`; clears the environment |
| `/usr/local/libexec/councillogic/human-entry` | root, 0755 | `approve`, `decide`, `doctor`, `plan`, `inspect`, `export`, `list`, `providers` |
| `/usr/local/libexec/councillogic/read-key` | root, 0755 | `MODEL_COUNCIL_SECRET_COMMAND` for the service user |
| `/usr/local/bin/council-agent`, `council-mcp-agent` | root, 0755 | The only commands an agent account uses |
| `/etc/sudoers.d/councillogic` | root, 0440 | Lets `councillogic-agents` run exactly `agent-entry agent` and `agent-entry mcp` as `councillogic`, with no password |

## Install

```bash
cd scripts/service-user
sudo ./install.sh --wheel ../../dist/model_council-0.3.1a1-py3-none-any.whl --agent-user AGENT_ACCOUNT          # review
sudo ./install.sh --wheel ../../dist/model_council-0.3.1a1-py3-none-any.whl --agent-user AGENT_ACCOUNT --apply  # do it
sudo ./add-key.sh OPENAI_API_KEY     # hidden prompt; repeat per provider (also how you rotate)
```

The installer:
- creates the `councillogic` system user and group, and a `councillogic-agents` group (Linux: `useradd --system`; macOS: `dscl`, hidden account, UID below 500);
- adds the agent account to that group;
- installs the wheel into a root-owned venv;
- creates the 0700 directories;
- installs the entry scripts, wrappers and the sudoers rule (checked with `visudo -cf` first).

Keys live in files instead of the Keychain or 1Password. A system account has
no login Keychain or 1Password session.

## Use

**Agents** (the agent account, in group `councillogic-agents`):

```bash
echo '{"action":"plan","question":"..."}' | council-agent
```

For MCP clients, set `"command": "council-mcp-agent"` with no args.

**Humans** (your own account, with your sudo password):

```bash
sudo -u councillogic /usr/local/libexec/councillogic/human-entry doctor
sudo -u councillogic /usr/local/libexec/councillogic/human-entry approve --question "..." --plan-hash H --approved-by "Andrew"
sudo -u councillogic /usr/local/libexec/councillogic/human-entry decide RUN_ID --accept --note "..."
```

`approve` still needs an interactive terminal, and sudo keeps yours.

## What this protects, and what it doesn't

- **Protected.** The agent account can't:
  - read `approval.key` (so it can't mint approval tokens);
  - read provider keys, or open or modify `council.sqlite3` (so it can't forge decisions or approvals);
  - run `approve` or `decide`, or change the installed code.

  The sudoers rule allows two exact argument lists and resets the
  environment, and `agent-entry` runs `env -i`.
- **Not protected.**
  - A human or agent with full `sudo` or root can do anything.
  - The agent can still spend within approved plans: any token a human hands it, once.
  - The agent can still start mock runs and fill the disk with them.
  - Provider keys are plaintext files guarded by Unix permissions only. Use full-disk encryption.
- **Single writer.** One run or start executes at a time per data folder; a concurrent one gets `"error": "busy"`. Read actions (status, inspect, list, plan) work during a run.

## Uninstall

On Linux: `userdel councillogic` and `groupdel councillogic-agents`. On macOS: `dscl . -delete /Users/councillogic` and remove the two groups.

Then remove:
- `/etc/sudoers.d/councillogic`
- `/usr/local/bin/council-agent` and `/usr/local/bin/council-mcp-agent`
- `/usr/local/libexec/councillogic`
- `/opt/councillogic`
- `/var/lib/councillogic` (export runs first: this contains every run and decision)
