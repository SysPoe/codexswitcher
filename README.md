# Codex Switcher

`codexswitcher.py` is a standalone helper for switching Codex local provider and account context. It snapshots the files Codex uses for durable provider/auth state:

- `~/.codex/config.toml`
- `~/.codex/auth.json` when file credential storage is used

It does not copy history, rollouts, logs, memories, or SQLite state.

## Why this shape

Codex 0.139.0 reads user configuration from `CODEX_HOME/config.toml`, which defaults to `~/.codex/config.toml`. ChatGPT/API login credentials are cached in `CODEX_HOME/auth.json` when `cli_auth_credentials_store = "file"` is active. Custom providers are selected with `model_provider` and `[model_providers.<id>]` entries.

The switcher stores named contexts under `~/.codex-switcher/contexts/<name>/` and installs a selected context by copying its `config.toml` and optional `auth.json` into `CODEX_HOME`.

## What Was Reverse Engineered

From the installed `codex-cli 0.139.0`, the official Codex manual, and `codex doctor --json`:

- `CODEX_HOME` is the root for local Codex config, auth, logs, sessions, and SQLite state.
- The active provider is controlled by top-level `model_provider`; `codexswitcher.py use` preserves your current `model` and `model_reasoning_effort`.
- Your current provider is `packycode`, defined in `[model_providers.packycode]` with `base_url = "https://www.packyapi.com/v1"` and `wire_api = "responses"`.
- `codex login status` currently reports no ChatGPT/API-key login, which is expected while `packycode` is active because that provider does not require OpenAI auth.
- `codex doctor` reports auth storage mode `File`, with the auth file path at `~/.codex/auth.json`.
- User-level `~/.codex/config.toml` is the right place for provider switching. Project `.codex/config.toml` files cannot override `model_provider` or `model_providers`.

## Quick Start

Open the selector:

```bash
python3 codexswitcher.py
```

The selector displays every saved context immediately, then refreshes five-hour and weekly Codex usage in the background when a context is signed in with ChatGPT/OpenAI auth. Use Up/Down or `j`/`k`, press Enter to activate one, press `n` to add a new context, or press `q`/Esc to quit. After activating, adding, or deleting a context, the selector refreshes and returns to the main screen.

Save your current Packy config:

```bash
python3 codexswitcher.py capture packy
```

Switch back to it later:

```bash
python3 codexswitcher.py use packy
```

Create a built-in OpenAI/ChatGPT context and sign in:

```bash
python3 codexswitcher.py login personal-chatgpt --device-auth --use
```

For a browser login instead of device-code login:

```bash
python3 codexswitcher.py login personal-chatgpt --use
```

From the TUI, press `n` and choose one of:

- ChatGPT browser login, equivalent to `codex login`
- ChatGPT device-code login for SSH/headless sessions, equivalent to `codex login --device-auth`
- OpenAI API-key login for OpenAI endpoints, equivalent to `codex login --with-api-key`
- Codex access-token login, equivalent to `codex login --with-access-token`
- Custom provider API key, then provider id, API base URL, model, and wire API. Responses providers default to HTTPS transport.

To save an existing custom provider such as PackyAPI, you can capture a known-working Codex config:

```bash
python3 codexswitcher.py capture packy-api
```

To create one manually, pass the base URL explicitly:

```bash
python3 codexswitcher.py provider packy-api --provider-id packycode --model gpt-5.5 --base-url https://www.packyapi.com/v1 --api-key "$PACKY_API_KEY"
```

For OpenAI API-key login contexts, you can also store a custom OpenAI base URL:

```bash
python3 codexswitcher.py login packy-openai-compatible --with-api-key --base-url https://www.packyapi.com/v1 --use
```

If the provider id already exists in your active `config.toml`, you can omit `--base-url` and the switcher will reuse the saved provider settings while replacing the secret.

Custom provider contexts created by the switcher set `supports_websockets = false` by default, which prevents Codex from trying the Responses WebSocket endpoint before falling back to HTTPS. Pass `--supports-websockets` only for providers that implement Codex's Responses WebSocket transport, or `--no-supports-websockets` to force an existing provider context back to HTTPS.

List and inspect contexts:

```bash
python3 codexswitcher.py list
python3 codexswitcher.py status
```

Run Codex once with an isolated context without modifying `~/.codex`:

```bash
python3 codexswitcher.py run personal-chatgpt -- codex login status
```

## Notes

- Prefer `--env-key` over `--api-key` for provider secrets when possible.
- `login` forces `cli_auth_credentials_store = "file"` inside the saved context so each ChatGPT account gets its own `auth.json`.
- Usage limits are fetched with Codex's app-server `account/rateLimits/read` method for signed-in Codex/OpenAI-auth contexts. `list` and `status` fetch before printing; the TUI shows cached/placeholder values first and updates the screen as fresh values arrive. Custom provider-key contexts show `n/a` because ChatGPT plan limits do not apply to those providers.
- `use` always backs up the current `config.toml` and `auth.json` under `~/.codex-switcher/backups/`.
- Saved `config.toml`, `auth.json`, and backup files are written owner-only (`0600`) because provider configs can contain bearer tokens.
- If the Codex app or app-server daemon is already running, restart it after switching contexts so it reloads auth/config.
- If a context has no `auth.json`, `use` removes the active `CODEX_HOME/auth.json` unless you pass `--keep-auth`.
