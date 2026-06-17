# Codex Switcher

A small helper for saving and switching between local Codex contexts.

Use it when you want to keep separate Codex accounts or provider settings and switch between them quickly.

## Quick Start

Open the selector on macOS/Linux:

```bash
python3 codexswitcher.py
```

On Windows, use the native PowerShell entry point (Python is not required):

```powershell
.\codexswitcher.ps1
```

You can also run `codexswitcher.cmd` from PowerShell or Command Prompt.

Install the short `cdxsw` command into `~/.local/bin`:

```powershell
.\install-windows.ps1
cdxsw status
```

The installer creates launchers for PowerShell, Command Prompt, and Git Bash.

Use Up/Down to choose a context, Enter to activate it, `n` to add a new
context, and `q` or Esc to quit. The Python selector also supports `j`/`k`.

Save your current Codex setup as a context:

```bash
python3 codexswitcher.py capture work
```

On Windows, replace `python3 codexswitcher.py` in the examples with
`.\codexswitcher.ps1`.

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
- The Windows CLI and Codex app share `%USERPROFILE%\.codex` by default.
- Restart the Codex app after switching contexts. On Windows, pass `--restart-app`
  to `use` or `login --use` to stop and relaunch the installed app automatically.
- The native Windows entry point supports `tui`, `capture`, `use`, `login`,
  `run`, `list`, and `status`. Custom `provider` creation remains available
  through the Python entry point.
- Saved contexts and backups live under `~/.codex-switcher/`.
