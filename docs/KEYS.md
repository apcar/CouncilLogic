# Adding and rotating API keys

CouncilLogic never stores keys. It reads each key into memory at run time and
looks in this order, using the first source that has it:

1. `MODEL_COUNCIL_SECRET_COMMAND`: your own resolver executable (absolute path).
2. **macOS Keychain**, when `MODEL_COUNCIL_KEYCHAIN_SERVICE` is set.
3. **1Password CLI**, for each key with a `<KEY_NAME>_OP_REF` reference.
4. **Environment variables**, such as `OPENAI_API_KEY`.

There are no Python dependencies: Keychain and 1Password are read through
`/usr/bin/security` and `op` subprocesses with a minimal environment and a
timeout. Key values are never written to disk, the run database, logs, or
error messages, and provider objects show `api_key='[REDACTED]'`.

Key names: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`MISTRAL_API_KEY`, `XAI_API_KEY`, `DASHSCOPE_API_KEY`, `COHERE_API_KEY`,
`UPSTAGE_API_KEY`.

## Add a key

### macOS Keychain (recommended on a Mac)

```bash
# -U updates an existing item. Leaving -w last makes `security` prompt for the
# value, so it doesn't land in shell history.
security add-generic-password -U -s councillogic -a OPENAI_API_KEY -w
export MODEL_COUNCIL_KEYCHAIN_SERVICE=councillogic   # e.g. in ~/.zshrc
council doctor
```

`doctor` should show `macOS Keychain (service councillogic)` as the source.
It also reads the item's modification date, so the key's age is shown
automatically.

### 1Password

```bash
op signin                                     # or enable the desktop-app integration
export OPENAI_API_KEY_OP_REF="op://AI/OpenAI/credential"
council doctor
```

The reference is not a secret; the key is read with `op read` when needed.
To show the key's age, also set `OPENAI_API_KEY_ROTATED_AT=YYYY-MM-DD`.

### Environment variable

```bash
read -rs OPENAI_API_KEY && export OPENAI_API_KEY   # avoids shell history
export OPENAI_API_KEY_ROTATED_AT=2026-10-19         # optional, enables age
```

`.env` files are deliberately not loaded.

## Check keys

- `council doctor` makes **no network calls**. It shows each provider's key
  source, the key age if known, a rotation reminder (policy: 90 days), and
  that provider's key-management page.
- `council doctor --check-keys` makes **one free "list models" request** per
  provider that has a key. It reports `valid`, `invalid` (HTTP 401/403, which
  also marks doctor NOT READY), or `error`. A valid key does not prove that
  the pinned model is available to your account.

## Rotate a key

1. Create a new key on the provider's page (`doctor` prints it):
   OpenAI <https://platform.openai.com/api-keys>,
   Anthropic <https://console.anthropic.com/settings/keys>,
   Google AI Studio <https://aistudio.google.com/app/apikey>,
   Mistral <https://console.mistral.ai/api-keys>,
   xAI <https://console.x.ai>,
   Alibaba Model Studio <https://modelstudio.console.alibabacloud.com/>,
   Cohere <https://dashboard.cohere.com/api-keys>,
   Upstage <https://console.upstage.ai/api-keys>.
2. Replace the stored value: rerun `security add-generic-password -U ...`,
   edit the 1Password item, or re-export the variable. Update
   `<KEY_NAME>_ROTATED_AT` if you use it.
3. Run `council doctor --check-keys` and confirm `valid`.
4. Revoke the old key on the provider's page.
