# Codex Switcher

A small helper for saving and switching between local Codex contexts.

Use it when you want to keep separate Codex accounts or provider settings and switch between them quickly.

## Quick Start

Open the selector:

```bash
python3 codexswitcher.py
```

Use Up/Down or `j`/`k` to choose a context, Enter to activate it, `n` to add a new context, and `q` or Esc to quit.

Save your current Codex setup as a context:

```bash
python3 codexswitcher.py capture work
```

Switch to a saved context:

```bash
python3 codexswitcher.py use work
```

Create a new ChatGPT/OpenAI login context and activate it:

```bash
python3 codexswitcher.py login personal --device-auth --use
```

For browser login instead:

```bash
python3 codexswitcher.py login personal --use
```

Create a custom provider context:

```bash
python3 codexswitcher.py provider my-provider --provider-id my-provider --model gpt-5 --base-url https://example.com/v1 --api-key "$API_KEY"
```

List saved contexts and show the active status:

```bash
python3 codexswitcher.py list
python3 codexswitcher.py status
```

Run one command with a saved context without switching your active setup:

```bash
python3 codexswitcher.py run personal -- codex login status
```

## Tips

- Use `--env-key` instead of `--api-key` when you want secrets to come from an environment variable.
- Restart Codex after switching contexts if another Codex process is already running.
- Saved contexts and backups live under `~/.codex-switcher/`.
