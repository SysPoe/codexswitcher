#!/usr/bin/env python3
"""Switch Codex CLI/app auth and provider contexts.

The script intentionally snapshots only the Codex files that control auth and
provider selection: config.toml and auth.json. Other state such as history,
rollouts, logs, and SQLite databases remains in the normal CODEX_HOME.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10.
    tomllib = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None  # type: ignore[assignment]


CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BUILTIN_PROVIDERS = {"openai", "ollama", "lmstudio", "amazon-bedrock"}
TUI_POPUP_SECONDS = 3.0


class SwitcherError(RuntimeError):
    """Expected command failure with a user-facing message."""


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        store = expand_path(args.store)
        codex_home = expand_path(args.codex_home)
        switcher = Switcher(store=store, codex_home=codex_home, codex_bin=args.codex_bin)
        with switcher.lock():
            return args.func(switcher, args)
    except SwitcherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot and switch Codex provider/account contexts.",
    )
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", "~/.codex"),
        help="Codex home to switch. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--store",
        default="~/.codex-switcher",
        help="Directory where contexts and backups are stored.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable used by login/run helpers.",
    )

    parser.set_defaults(func=cmd_tui)
    subparsers = parser.add_subparsers(dest="command")

    capture = subparsers.add_parser(
        "capture",
        aliases=["save"],
        help="Save the current CODEX_HOME config/auth as a named context.",
    )
    capture.add_argument("name", help="Context name, e.g. custom-context or work-chatgpt.")
    capture.add_argument("--overwrite", action="store_true", help="Replace an existing context.")
    capture.set_defaults(func=cmd_capture)

    use = subparsers.add_parser(
        "use",
        help="Install a saved context into CODEX_HOME.",
    )
    use.add_argument("name", help="Context to activate.")
    use.add_argument(
        "--keep-auth",
        action="store_true",
        help="Keep the current auth.json when the selected context has no auth.json.",
    )
    use.add_argument(
        "--restart-app",
        action="store_true",
        help="Restart the Windows Codex app after switching.",
    )
    use.set_defaults(func=cmd_use)

    provider = subparsers.add_parser(
        "provider",
        help="Create a provider context from the current config plus provider overrides.",
    )
    provider.add_argument("name", help="Context name to create/update.")
    provider.add_argument(
        "--provider-id",
        required=True,
        help="Codex model_provider id. Use openai for the built-in OpenAI provider.",
    )
    provider.add_argument("--model", required=True, help="Default model for this context.")
    provider.add_argument("--provider-name", help="Human-friendly provider name.")
    provider.add_argument("--base-url", help="Provider base URL, e.g. https://example/v1.")
    provider.add_argument(
        "--wire-api",
        help="responses or chat_completions. Defaults to the existing provider setting or responses over HTTPS.",
    )
    websocket = provider.add_mutually_exclusive_group()
    websocket.add_argument(
        "--supports-websockets",
        dest="supports_websockets",
        action="store_true",
        default=None,
        help="Opt a custom Responses provider into Codex WebSocket transport. Custom providers default to HTTPS.",
    )
    websocket.add_argument(
        "--no-supports-websockets",
        dest="supports_websockets",
        action="store_false",
        default=None,
        help="Force HTTPS transport for a custom Responses provider.",
    )
    provider.add_argument("--env-key", help="Environment variable containing the provider API key.")
    provider.add_argument("--api-key", help="Provider API key to store in this context.")
    provider.add_argument(
        "--requires-openai-auth",
        action="store_true",
        help="Use Codex/OpenAI auth for this provider.",
    )
    provider.add_argument(
        "--reasoning-effort",
        help="Optional model_reasoning_effort value for this context.",
    )
    provider.add_argument("--overwrite", action="store_true", help="Replace an existing context.")
    provider.add_argument("--use", action="store_true", help="Install the context into CODEX_HOME after creating it.")
    provider.add_argument(
        "--restart-app",
        action="store_true",
        help="Restart the Windows Codex app after activating with --use.",
    )
    provider.set_defaults(func=cmd_provider)

    login = subparsers.add_parser(
        "login",
        help="Create/update a context by running Codex login in an isolated CODEX_HOME.",
    )
    login.add_argument("name", help="Context that should receive the new auth.json.")
    login.add_argument(
        "--provider-id",
        default="openai",
        help="Provider to select while logging in. Defaults to built-in openai.",
    )
    login.add_argument("--model", default="gpt-5.5", help="Model to put in the login context.")
    login.add_argument(
        "--base-url",
        help="Optional API base URL to store in the login context, e.g. https://api.example.test/v1.",
    )
    login.add_argument("--device-auth", action="store_true", help="Pass --device-auth to codex login.")
    auth_source = login.add_mutually_exclusive_group()
    auth_source.add_argument(
        "--with-api-key",
        action="store_true",
        help="Pass stdin through to codex login --with-api-key.",
    )
    auth_source.add_argument(
        "--with-access-token",
        action="store_true",
        help="Pass stdin through to codex login --with-access-token.",
    )
    login.add_argument(
        "--use",
        action="store_true",
        help="Install the context into CODEX_HOME after a successful login.",
    )
    login.add_argument(
        "--restart-app",
        action="store_true",
        help="Restart the Windows Codex app after activating with --use.",
    )
    login.set_defaults(func=cmd_login)

    run = subparsers.add_parser(
        "run",
        help="Run a command with CODEX_HOME set to an isolated copy of a context.",
    )
    run.add_argument("name", help="Context to run with.")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="Command after --, e.g. -- codex status.")
    run.set_defaults(func=cmd_run)

    list_cmd = subparsers.add_parser("list", help="List saved contexts.")
    list_cmd.set_defaults(func=cmd_list)

    tui = subparsers.add_parser("tui", help="Open the interactive context selector.")
    tui.set_defaults(func=cmd_tui)

    status = subparsers.add_parser("status", help="Show active CODEX_HOME and selected provider.")
    status.set_defaults(func=cmd_status)

    drop_auth = subparsers.add_parser("drop-auth", help="Remove auth.json from a saved context.")
    drop_auth.add_argument("name", help="Context to edit.")
    drop_auth.set_defaults(func=cmd_drop_auth)

    return parser


def cmd_capture(switcher: "Switcher", args: argparse.Namespace) -> int:
    switcher.capture(args.name, overwrite=args.overwrite)
    print(f"saved context {args.name!r} from {switcher.codex_home}")
    return 0


def cmd_use(switcher: "Switcher", args: argparse.Namespace) -> int:
    switcher.use(args.name, keep_auth=args.keep_auth)
    print(f"activated context {args.name!r} in {switcher.codex_home}")
    if args.restart_app:
        switcher.restart_codex_app()
    elif switcher.app_server_may_be_running():
        print("note: Codex app/app-server may need a restart to pick up auth/config changes.")
    return 0


def cmd_provider(switcher: "Switcher", args: argparse.Namespace) -> int:
    switcher.create_provider_context(
        name=args.name,
        provider_id=args.provider_id,
        model=args.model,
        provider_name=args.provider_name,
        base_url=args.base_url,
        wire_api=args.wire_api,
        supports_websockets=args.supports_websockets,
        env_key=args.env_key,
        api_key=args.api_key,
        requires_openai_auth=args.requires_openai_auth,
        reasoning_effort=args.reasoning_effort,
        overwrite=args.overwrite,
    )
    print(f"created provider context {args.name!r}")
    if args.use:
        switcher.use(args.name)
        print(f"activated context {args.name!r} in {switcher.codex_home}")
        if args.restart_app:
            switcher.restart_codex_app()
        elif switcher.app_server_may_be_running():
            print("note: Codex app/app-server may need a restart to pick up auth/config changes.")
    return 0


def cmd_login(switcher: "Switcher", args: argparse.Namespace) -> int:
    switcher.login(
        name=args.name,
        provider_id=args.provider_id,
        model=args.model,
        base_url=args.base_url,
        device_auth=args.device_auth,
        with_api_key=args.with_api_key,
        with_access_token=args.with_access_token,
        activate=args.use,
    )
    print(f"stored login credentials in context {args.name!r}")
    if args.use:
        print(f"activated context {args.name!r} in {switcher.codex_home}")
        if args.restart_app:
            switcher.restart_codex_app()
        elif switcher.app_server_may_be_running():
            print("note: Codex app/app-server may need a restart to pick up auth/config changes.")
    return 0


def cmd_run(switcher: "Switcher", args: argparse.Namespace) -> int:
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise SwitcherError("run requires a command after --")
    return switcher.run(args.name, cmd)


def cmd_list(switcher: "Switcher", args: argparse.Namespace) -> int:
    del args
    rows = switcher.list_contexts(refresh_limits=True)
    if not rows:
        print(f"no contexts saved in {switcher.store}")
        return 0
    print(format_context_table(rows))
    return 0


def cmd_tui(switcher: "Switcher", args: argparse.Namespace) -> int:
    del args
    rows = switcher.list_contexts(refresh_limits=False)
    if not rows:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(f"no contexts saved in {switcher.store}")
            print(
                "Create one with `python3 codexswitcher.py capture custom-context` "
                "or `python3 codexswitcher.py login personal-chatgpt`."
            )
            return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(format_context_table(rows))
        print("non-interactive shell detected; use `codexswitcher.py use <name>` to activate a context.")
        return 0

    if os.name == "nt":
        return windows_tui(switcher, message="")

    message = ""
    while True:
        rows = switcher.list_contexts(refresh_limits=False)
        action = select_context_tui(rows, switcher, message)
        if action is None:
            print("cancelled")
            return 130
        try:
            message = execute_tui_action(switcher, action)
        except SwitcherError as exc:
            message = f"error: {exc}"


def execute_tui_action(switcher: "Switcher", action: dict[str, str]) -> str:
    if action["action"] == "login":
        secret_input = prompt_login_secret(action["mode"])
        base_url = prompt_login_base_url(action["mode"])
        switcher.login(
            name=action["name"],
            provider_id="openai",
            model="gpt-5.5",
            base_url=base_url,
            device_auth=action["mode"] == "device",
            with_api_key=action["mode"] == "api-key",
            with_access_token=action["mode"] == "access-token",
            activate=True,
            secret_input=secret_input,
        )
        message = f"stored and activated login context {action['name']!r}"
    elif action["action"] == "provider":
        switcher.create_provider_context(
            name=action["name"],
            provider_id=action["provider_id"],
            model=action["model"],
            provider_name=action["provider_name"],
            base_url=action["base_url"],
            wire_api=action["wire_api"],
            supports_websockets=None,
            env_key=action.get("env_key"),
            api_key=action.get("api_key"),
            requires_openai_auth=False,
            reasoning_effort=None,
            overwrite=False,
        )
        switcher.use(action["name"])
        message = f"stored and activated provider context {action['name']!r}"
    elif action["action"] == "delete":
        switcher.delete_context(action["name"])
        return f"deleted context {action['name']!r}"
    else:
        switcher.use(action["name"])
        message = f"activated context {action['name']!r}"

    if switcher.app_server_may_be_running():
        message += "; restart Codex app/app-server to pick up changes"
    return message


def prompt_login_secret(mode: str) -> str | None:
    if mode not in {"api-key", "access-token"}:
        return None
    import getpass

    if mode == "api-key":
        label = "OpenAI API key"
    else:
        label = "Codex access token"
    secret = getpass.getpass(f"{label}: ")
    if not secret.strip():
        raise SwitcherError(f"{label} cannot be empty")
    return secret.strip() + "\n"


def prompt_login_base_url(mode: str) -> str | None:
    if mode != "api-key":
        return None
    base_url = input("OpenAI API base URL (blank for OpenAI default): ").strip()
    return base_url or None


def format_context_table(rows: list[dict[str, str]]) -> str:
    widths = {
        "active": 1,
        "name": max(4, max(len(row["name"]) for row in rows)),
        "model": max(5, max(len(row["model"]) for row in rows)),
        "provider": max(8, max(len(row["provider"]) for row in rows)),
        "auth": max(4, max(len(row["auth"]) for row in rows)),
        "provider_auth": max(13, max(len(row["provider_auth"]) for row in rows)),
        "five_hour": max(2, max(len(row["five_hour"]) for row in rows)),
        "weekly": max(6, max(len(row["weekly"]) for row in rows)),
    }
    header = (
        f"{' ':<{widths['active']}}  "
        f"{'name':<{widths['name']}}  "
        f"{'model':<{widths['model']}}  "
        f"{'provider':<{widths['provider']}}  "
        f"{'auth':<{widths['auth']}}  "
        f"{'provider-auth':<{widths['provider_auth']}}  "
        f"{'5h':<{widths['five_hour']}}  "
        f"{'weekly':<{widths['weekly']}}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['active']:<{widths['active']}}  "
            f"{row['name']:<{widths['name']}}  "
            f"{row['model']:<{widths['model']}}  "
            f"{row['provider']:<{widths['provider']}}  "
            f"{row['auth']:<{widths['auth']}}  "
            f"{row['provider_auth']:<{widths['provider_auth']}}  "
            f"{row['five_hour']:<{widths['five_hour']}}  "
            f"{row['weekly']:<{widths['weekly']}}"
        )
    return "\n".join(lines)


def windows_tui(switcher: "Switcher", message: str = "") -> int:
    import msvcrt

    selected = 0
    top = 0
    last_frame_height = terminal_size()[1]
    refresh_queue: Queue[tuple[str | None, dict[str, str]]] | None = None
    fetching_limits = False
    popup_expires_at = time.monotonic() + TUI_POPUP_SECONDS if message else 0.0

    def show_popup(text: str) -> None:
        nonlocal message, popup_expires_at
        message = text
        popup_expires_at = time.monotonic() + TUI_POPUP_SECONDS

    def clear_popup() -> None:
        nonlocal message, popup_expires_at
        message = ""
        popup_expires_at = 0.0

    sys.stdout.write("\x1b[?25l\x1b[2J")
    sys.stdout.flush()
    try:
        while True:
            rows = switcher.list_contexts(refresh_limits=False)
            if refresh_queue is None:
                refresh_queue = start_limit_refresh(switcher, rows)
                fetching_limits = has_refreshable_limits(rows)
            if refresh_queue is not None:
                updated, finished_refresh = apply_limit_refresh_updates(rows, refresh_queue)
                if finished_refresh:
                    refresh_queue = None
                    fetching_limits = False
                if updated and not message:
                    show_popup("Rate limits updated.")

            width, height = terminal_size()
            detail_lines = 8
            list_height = max(1, height - detail_lines - 5)
            if rows:
                selected = max(0, min(selected, len(rows) - 1))
            else:
                selected = 0
            if selected < top:
                top = selected
            elif selected >= top + list_height:
                top = selected - list_height + 1
            if message and time.monotonic() >= popup_expires_at:
                clear_popup()

            last_frame_height = height
            lines = render_tui_lines(rows, switcher, selected, top, list_height, width, height)
            if message:
                lines[-1] = centered_popup(message, width)
            elif fetching_limits:
                lines[-1] = fit_line("Fetching rate limits...", width)
            else:
                lines[-1] = fit_line("Ready.", width)
            write_windows_frame(lines)

            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            key = read_windows_key(msvcrt)
            if key in {"up", "k"}:
                clear_popup()
                selected -= 1
            elif key in {"down", "j"}:
                clear_popup()
                selected += 1
            elif key in {"pagedown", "space"}:
                clear_popup()
                selected += list_height
            elif key == "pageup":
                clear_popup()
                selected -= list_height
            elif key in {"home", "g"}:
                clear_popup()
                selected = 0
            elif key in {"end", "G"}:
                clear_popup()
                selected = len(rows) - 1
            elif key == "enter":
                if rows:
                    name = rows[selected]["name"]
                    try:
                        switcher.use(name)
                        text = f"Activated context {name!r}. Restart Codex app to apply it."
                    except SwitcherError as exc:
                        text = f"error: {exc}"
                    show_popup(text)
                else:
                    show_popup("No context selected. Press n to create one.")
            elif key == "n":
                action = prompt_new_context_windows()
                if action is not None:
                    show_popup(execute_windows_tui_action(switcher, action))
                    refresh_queue = None
                    selected = next(
                        (index for index, row in enumerate(switcher.list_contexts(False)) if row["name"] == action["name"]),
                        selected,
                    )
            elif key == "delete":
                if rows:
                    name = rows[selected]["name"]
                    if confirm_windows(f"Delete {name!r}? y/N "):
                        switcher.delete_context(name)
                        show_popup(f"Deleted context {name!r}.")
                        refresh_queue = None
                        selected = max(0, selected - 1)
                    else:
                        show_popup("Delete cancelled.")
            elif key == "r":
                refresh_queue = start_limit_refresh(switcher, rows)
                fetching_limits = has_refreshable_limits(rows)
                if fetching_limits:
                    show_popup("Fetching rate limits for all contexts...")
                else:
                    show_popup("No refreshable Codex/OpenAI contexts.")
            elif key in {"escape", "q"}:
                return 0
            elif key:
                show_popup("Unknown key. Enter activates, n adds, Delete removes, q exits.")
    finally:
        sys.stdout.write(windows_tui_exit_sequence(last_frame_height))
        sys.stdout.flush()


def render_tui_lines(
    rows: list[dict[str, str]],
    switcher: "Switcher",
    selected: int,
    top: int,
    list_height: int,
    width: int,
    height: int,
) -> list[str]:
    lines = [" " * width for _ in range(height)]
    put_line(lines, 0, "Codex Switcher", width)
    put_line(lines, 1, f"CODEX_HOME: {switcher.codex_home}", width)
    put_line(lines, 2, "Enter: activate  Delete: delete  n: new  r: refresh  Up/Down or j/k: move  q/Esc: quit", width)
    put_line(lines, 3, "-" * width, width)
    if not rows:
        put_line(lines, 4, "No saved contexts. Press n to create one.", width)
    else:
        for offset, row in enumerate(rows[top : top + list_height]):
            index = top + offset
            active = "*" if row["active"] else " "
            marker = ">" if index == selected else " "
            text = (
                f"{active}{marker} {row['name']:<22.22} {row['model']:<14.14} "
                f"{row['provider']:<12.12} auth:{row['auth']:<3} "
                f"5h:{row['five_hour']:<10.10} wk:{row['weekly']:<10.10}"
            )
            put_line(lines, 4 + offset, text, width)
    detail_y = min(max(4, height - 9), 5 + list_height)
    put_line(lines, detail_y, "-" * width, width)
    if rows:
        row = rows[selected]
        detail = [
            f"name:          {row['name']}",
            f"model:         {row['model']}",
            f"provider:      {row['provider']}",
            f"auth:          {row['auth']}",
            f"provider auth: {row['provider_auth']}",
            f"five-hour:     {row['five_hour_detail']}",
            f"weekly:        {row['weekly_detail']}",
        ]
    else:
        detail = ["Press n to sign in with ChatGPT and save the account as a context."]
    for offset, text in enumerate(detail, start=1):
        put_line(lines, detail_y + offset, text, width)
    return lines


def terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(100, 30))
    return max(30, size.columns - 1), max(12, size.lines)


def put_line(lines: list[str], y: int, text: str, width: int) -> None:
    if 0 <= y < len(lines):
        lines[y] = fit_line(text, width)


def fit_line(text: str, width: int) -> str:
    return text[:width].ljust(width)


def centered_popup(text: str, width: int) -> str:
    popup = f" {text} "
    if len(popup) > width:
        popup = popup[:width]
    return popup.center(width)


def write_windows_frame(lines: list[str]) -> None:
    frame = ["\x1b[?2026h"]
    for index, line in enumerate(lines, start=1):
        frame.append(f"\x1b[{index};1H{line}\x1b[0m")
    frame.append("\x1b[1;1H\x1b[?2026l")
    sys.stdout.write("".join(frame))
    sys.stdout.flush()


def windows_tui_exit_sequence(frame_height: int) -> str:
    row = max(1, frame_height)
    return f"\x1b[?2026l\x1b[?25h\x1b[0m\x1b[{row};1H\x1b[0K\n"


def read_windows_key(msvcrt_module: Any) -> str:
    raw = msvcrt_module.getwch()
    if raw in ("\x00", "\xe0"):
        extended = msvcrt_module.getwch()
        return {
            "H": "up",
            "P": "down",
            "K": "left",
            "M": "right",
            "S": "delete",
            "I": "pageup",
            "Q": "pagedown",
            "G": "home",
            "O": "end",
        }.get(extended, "")
    return {
        "\r": "enter",
        "\x1b": "escape",
        " ": "space",
        "\x08": "backspace",
    }.get(raw, raw)


def prompt_new_context_windows() -> dict[str, str] | None:
    print("\x1b[?25h\x1b[2J\x1b[1;1H", end="", flush=True)
    name = input("New context name: ").strip()
    if not name:
        return None
    validate_context_name(name)
    options = [
        ("browser", "ChatGPT browser login"),
        ("device", "ChatGPT device-code login"),
        ("api-key", "OpenAI API key"),
        ("provider", "Custom provider API endpoint and key"),
    ]
    for index, (_, label) in enumerate(options, start=1):
        print(f"{index}. {label}")
    choice = input("Choose context type: ").strip()
    try:
        mode = options[int(choice) - 1][0]
    except (ValueError, IndexError):
        return None
    if mode == "provider":
        api_key = read_windows_secret("Provider API key: ").strip()
        if not api_key:
            raise SwitcherError("Provider API key cannot be empty")
        action = prompt_provider_config_windows(name)
        if action is not None:
            action["api_key"] = api_key
        return action
    return {"action": "login", "name": name, "mode": mode}


def prompt_provider_config_windows(name: str) -> dict[str, str] | None:
    provider_id = input("Provider id (for example customapi): ").strip()
    validate_provider_id(provider_id)
    provider_name = input(f"Provider display name (default {provider_id}): ").strip()
    base_url = input("Provider API base URL: ").strip()
    if not base_url:
        raise SwitcherError("Provider API base URL is required")
    model = input("Model (default gpt-5.5): ").strip() or "gpt-5.5"
    wire_api = input("Wire API (default responses): ").strip() or "responses"
    return {
        "action": "provider",
        "name": name,
        "provider_id": provider_id,
        "provider_name": provider_name or provider_id,
        "base_url": base_url,
        "model": model,
        "wire_api": wire_api,
    }


def execute_windows_tui_action(switcher: "Switcher", action: dict[str, str]) -> str:
    try:
        return execute_tui_action(switcher, action)
    except SwitcherError as exc:
        return f"error: {exc}"


def confirm_windows(prompt: str) -> bool:
    print("\x1b[?25h\x1b[2J\x1b[1;1H", end="", flush=True)
    return input(prompt).strip().lower() == "y"


def read_windows_secret(prompt: str) -> str:
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars: list[str] = []
    while True:
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            sys.stdout.write("\n")
            sys.stdout.flush()
            return ""
        if char == "\b":
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if char in ("\x00", "\xe0"):
            with contextlib.suppress(Exception):
                msvcrt.getwch()
            continue
        chars.append(char)
        sys.stdout.write("*")
        sys.stdout.flush()


def select_context_tui(rows: list[dict[str, str]], switcher: "Switcher", initial_message: str = "") -> None:
    import curses

    def run(stdscr: Any) -> None:
        nonlocal rows

        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        stdscr.keypad(True)
        selected = next((index for index, row in enumerate(rows) if row["active"] == "*"), 0)
        top = 0
        popup_message = initial_message
        popup_expires_at = time.monotonic() + TUI_POPUP_SECONDS if popup_message else 0.0
        refresh_queue = start_limit_refresh(switcher, rows)
        fetching_limits = has_refreshable_limits(rows)
        stdscr.timeout(200)

        def show_popup(text: str) -> None:
            nonlocal popup_message, popup_expires_at
            popup_message = text
            popup_expires_at = time.monotonic() + TUI_POPUP_SECONDS

        def clear_popup() -> None:
            nonlocal popup_message, popup_expires_at
            popup_message = ""
            popup_expires_at = 0.0

        while True:
            updated, finished_refresh = apply_limit_refresh_updates(rows, refresh_queue)
            if finished_refresh:
                fetching_limits = False
            if updated:
                if not popup_message:
                    show_popup("Rate limits updated.")

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if popup_message and time.monotonic() >= popup_expires_at:
                clear_popup()
            detail_lines = 8
            bottom_reserved = 1 if height > 1 else 0
            list_height = max(1, height - detail_lines - bottom_reserved - 5)
            if rows:
                selected = max(0, min(selected, len(rows) - 1))
            else:
                selected = 0
            if selected < top:
                top = selected
            elif selected >= top + list_height:
                top = selected - list_height + 1

            title = "Codex Switcher"
            subtitle = f"CODEX_HOME: {switcher.codex_home}"
            help_text = "Enter: activate  Delete: delete  n: new context  Up/Down or j/k: move  q/Esc: quit"
            add_line(stdscr, 0, 0, title, width, curses.A_BOLD)
            add_line(stdscr, 1, 0, subtitle, width)
            add_line(stdscr, 2, 0, help_text, width)
            add_line(stdscr, 3, 0, "-" * max(0, width - 1), width)

            visible = rows[top : top + list_height]
            if not rows:
                add_line(stdscr, 4, 0, "No saved contexts. Press n to create a ChatGPT login context.", width)
            else:
                for offset, row in enumerate(visible):
                    index = top + offset
                    marker = ">" if index == selected else " "
                    active = "*" if row["active"] == "*" else " "
                    text = (
                        f"{marker}{active} {row['name']:<22.22} "
                        f"{row['model']:<14.14} "
                        f"{row['provider']:<12.12} "
                        f"auth:{row['auth']:<3} "
                        f"5h:{row['five_hour']:<10.10} "
                        f"wk:{row['weekly']:<10.10}"
                    )
                    attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
                    add_line(stdscr, 4 + offset, 0, text, width, attr)

            detail_y = min(height - detail_lines, 4 + list_height + 1)
            add_line(stdscr, detail_y, 0, "-" * max(0, width - 1), width)
            if rows:
                row = rows[selected]
                add_line(stdscr, detail_y + 1, 0, f"name:          {row['name']}", width, curses.A_BOLD)
                add_line(stdscr, detail_y + 2, 0, f"model:         {row['model']}", width)
                add_line(stdscr, detail_y + 3, 0, f"provider:      {row['provider']}", width)
                add_line(stdscr, detail_y + 4, 0, f"auth:          {row['auth']}", width)
                add_line(stdscr, detail_y + 5, 0, f"provider auth: {row['provider_auth']}", width)
                add_line(stdscr, detail_y + 6, 0, f"five-hour:     {row['five_hour_detail']}", width)
                add_line(stdscr, detail_y + 7, 0, f"weekly:        {row['weekly_detail']}", width)
            else:
                add_line(stdscr, detail_y + 1, 0, "Press n to sign in with ChatGPT and save the account as a context.", width)

            if popup_message:
                add_popup(stdscr, height - 1, popup_message, width, curses.A_REVERSE)
            elif fetching_limits:
                add_line(stdscr, height - 1, 0, "Fetching rate limits...", width, curses.A_BOLD)
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue
            if key in (curses.KEY_UP, ord("k")):
                clear_popup()
                selected -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                clear_popup()
                selected += 1
            elif key in (curses.KEY_NPAGE, ord(" ")):
                clear_popup()
                selected += list_height
            elif key == curses.KEY_PPAGE:
                clear_popup()
                selected -= list_height
            elif key in (curses.KEY_HOME, ord("g")):
                clear_popup()
                selected = 0
            elif key in (curses.KEY_END, ord("G")):
                clear_popup()
                selected = len(rows) - 1
            elif key in (10, 13, curses.KEY_ENTER):
                if rows:
                    action = {"action": "use", "name": rows[selected]["name"]}
                    show_popup(execute_tui_action_in_place(stdscr, switcher, action))
                    rows = switcher.list_contexts(refresh_limits=False)
                    refresh_queue = start_limit_refresh(switcher, rows)
                    fetching_limits = has_refreshable_limits(rows)
                    selected = next(
                        (index for index, row in enumerate(rows) if row["name"] == action["name"]),
                        selected,
                    )
                else:
                    show_popup("No context selected. Press n to create one.")
            elif key == ord("n"):
                clear_popup()
                login_action = prompt_new_login_tui(stdscr)
                if login_action is not None:
                    show_popup(execute_tui_action_in_place(stdscr, switcher, login_action))
                    rows = switcher.list_contexts(refresh_limits=False)
                    refresh_queue = start_limit_refresh(switcher, rows)
                    fetching_limits = has_refreshable_limits(rows)
                    selected = next(
                        (index for index, row in enumerate(rows) if row["name"] == login_action["name"]),
                        selected,
                    )
            elif key == curses.KEY_DC:
                clear_popup()
                if rows and confirm_delete_context_tui(stdscr, rows[selected]["name"]):
                    action = {"action": "delete", "name": rows[selected]["name"]}
                    show_popup(execute_tui_action_in_place(stdscr, switcher, action))
                    rows = switcher.list_contexts(refresh_limits=False)
                    refresh_queue = start_limit_refresh(switcher, rows)
                    fetching_limits = has_refreshable_limits(rows)
                    selected = min(selected, max(0, len(rows) - 1))
                else:
                    show_popup("Delete cancelled.")
            elif key in (27, ord("q")):
                return
            elif key == curses.KEY_RESIZE:
                clear_popup()
                continue
            else:
                show_popup("Unknown key. Use Enter to activate, Delete to delete, n to add, or q to cancel.")

    try:
        return curses.wrapper(run)
    except curses.error as exc:
        raise SwitcherError(f"failed to start terminal UI: {exc}") from exc


def start_limit_refresh(switcher: "Switcher", rows: list[dict[str, str]]) -> Queue[tuple[str | None, dict[str, str]]]:
    queue: Queue[tuple[str | None, dict[str, str]]] = Queue()
    names = [row["name"] for row in rows if is_refreshable_limit_row(row)]
    if not names:
        return queue

    remaining = len(names)
    remaining_lock = threading.Lock()

    def refresh_one(name: str) -> None:
        nonlocal remaining
        try:
            context_dir = switcher.require_context(name)
            config = read_toml(context_dir / "config.toml")
            summary = switcher.context_limit_summary(context_dir, config, refresh=True)
        except Exception as exc:
            summary = empty_limit_summary("unavailable", f"fetch failed: {exc}")
        finally:
            with remaining_lock:
                remaining -= 1
                finished = remaining == 0
        queue.put((name, summary))
        if finished:
            queue.put((None, {}))

    for name in names:
        thread = threading.Thread(target=refresh_one, args=(name,), name=f"codexswitcher-limit-{name}", daemon=True)
        thread.start()
    return queue


def apply_limit_refresh_updates(
    rows: list[dict[str, str]],
    queue: Queue[tuple[str | None, dict[str, str]]],
) -> tuple[bool, bool]:
    changed = False
    finished = False
    by_name = {row["name"]: row for row in rows}
    while True:
        try:
            name, summary = queue.get_nowait()
        except Empty:
            return changed, finished
        if name is None:
            finished = True
            continue
        row = by_name.get(name)
        if row is not None:
            row.update(summary)
            changed = True


def has_refreshable_limits(rows: list[dict[str, str]]) -> bool:
    return any(is_refreshable_limit_row(row) for row in rows)


def is_refreshable_limit_row(row: dict[str, str]) -> bool:
    return row.get("auth") == "yes" and row.get("provider_auth") == "codex-auth"


def execute_tui_action_in_place(stdscr: Any, switcher: "Switcher", action: dict[str, str]) -> str:
    import curses

    try:
        if action["action"] == "login":
            curses.def_prog_mode()
            curses.endwin()
            try:
                return execute_tui_action(switcher, action)
            finally:
                curses.reset_prog_mode()
                stdscr.refresh()
        return execute_tui_action(switcher, action)
    except SwitcherError as exc:
        return f"error: {exc}"


def confirm_delete_context_tui(stdscr: Any, name: str) -> bool:
    import curses

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        add_line(stdscr, 0, 0, "Delete Context", width, curses.A_BOLD)
        add_line(stdscr, 1, 0, f"Delete saved context {name!r}?", width)
        add_line(stdscr, 2, 0, "This removes the saved config/auth snapshot. Active CODEX_HOME files are unchanged.", width)
        add_line(stdscr, min(4, height - 1), 0, "Press y to delete, or n/q/Esc to cancel.", width, curses.A_BOLD)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), ord("q"), 27):
            return False
        if key == curses.KEY_RESIZE:
            continue


def prompt_new_login_tui(stdscr: Any) -> dict[str, str] | None:
    import curses

    name = read_curses_prompt(stdscr, "New context name: ")
    if name is None:
        return None
    name = name.strip()
    try:
        validate_context_name(name)
    except SwitcherError:
        show_curses_message(stdscr, "Invalid name. Use letters, numbers, dot, underscore, or hyphen.")
        return None

    modes = [
        ("browser", "ChatGPT browser login"),
        ("device", "ChatGPT device-code login for SSH/headless sessions"),
        ("api-key", "OpenAI API key for OpenAI endpoints"),
        ("provider", "Custom provider API key, e.g. Custom API"),
    ]
    selected = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        add_line(stdscr, 0, 0, "New Context", width, curses.A_BOLD)
        add_line(stdscr, 1, 0, f"context: {name}", width)
        add_line(stdscr, 2, 0, "Choose context type. Enter selects, q/Esc returns.", width)
        add_line(stdscr, 3, 0, "-" * max(0, width - 1), width)
        for index, (_, label) in enumerate(modes):
            marker = ">" if index == selected else " "
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            add_line(stdscr, 4 + index, 0, f"{marker} {label}", width, attr)
        add_line(stdscr, height - 2, 0, "Browser login opens ChatGPT. Device-code login prints a code for another browser.", width)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(modes) - 1, selected + 1)
        elif key in (10, 13, curses.KEY_ENTER):
            if modes[selected][0] == "provider":
                api_key = read_curses_secret_prompt(stdscr, "Provider API key: ")
                if api_key is None or not api_key.strip():
                    show_curses_message(stdscr, "Provider API key cannot be empty.")
                    return None
                return prompt_provider_config_tui(stdscr, name, api_key.strip())
            return {"action": "login", "name": name, "mode": modes[selected][0]}
        elif key in (27, ord("q")):
            return None


def prompt_provider_config_tui(stdscr: Any, name: str, api_key: str) -> dict[str, str] | None:
    provider_id = read_curses_prompt(stdscr, "Provider id (for example customapi): ")
    if provider_id is None:
        return None
    provider_id = provider_id.strip()
    try:
        validate_provider_id(provider_id)
    except SwitcherError:
        show_curses_message(stdscr, "Invalid provider id. Use letters, numbers, underscore, or hyphen.")
        return None

    provider_name = read_curses_prompt(stdscr, f"Provider display name (default {provider_id}): ")
    base_url = read_curses_prompt(stdscr, "Provider API base URL: ")
    if base_url is None or not base_url.strip():
        show_curses_message(stdscr, "Provider API base URL is required for custom providers.")
        return None
    model = read_curses_prompt(stdscr, "Model (default gpt-5.5): ")
    wire_api = read_curses_prompt(stdscr, "Wire API (default responses over HTTPS): ")
    return {
        "action": "provider",
        "name": name,
        "provider_id": provider_id,
        "provider_name": (provider_name or provider_id).strip() or provider_id,
        "base_url": base_url.strip(),
        "model": (model or "gpt-5.5").strip() or "gpt-5.5",
        "wire_api": (wire_api or "responses").strip() or "responses",
        "api_key": api_key,
    }


def read_curses_prompt(stdscr: Any, prompt: str) -> str | None:
    import curses

    curses.echo()
    with contextlib.suppress(curses.error):
        curses.curs_set(1)
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        add_line(stdscr, 0, 0, prompt, width, curses.A_BOLD)
        add_line(stdscr, 2, 0, "Press Enter to continue, or leave blank to cancel.", width)
        stdscr.move(0, min(len(prompt), max(0, width - 1)))
        stdscr.refresh()
        raw = stdscr.getstr(0, min(len(prompt), max(0, width - 1)), max(1, width - len(prompt) - 1))
        value = raw.decode("utf-8", errors="ignore")
        return value if value else None
    finally:
        curses.noecho()
        with contextlib.suppress(curses.error):
            curses.curs_set(0)


def read_curses_secret_prompt(stdscr: Any, prompt: str) -> str | None:
    import curses

    curses.noecho()
    stdscr.keypad(True)
    with contextlib.suppress(curses.error):
        curses.curs_set(1)
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        add_line(stdscr, 0, 0, prompt, width, curses.A_BOLD)
        add_line(stdscr, 2, 0, "Press Enter to continue, or leave blank to cancel.", width)
        input_x = min(len(prompt), max(0, width - 1))
        max_chars = max(1, width - input_x - 1)
        stdscr.move(0, input_x)
        stdscr.refresh()
        chars: list[str] = []
        while True:
            key = stdscr.get_wch()
            if key in ("\n", "\r") or key == curses.KEY_ENTER:
                return "".join(chars) if chars else None
            if key == "\x1b":
                return None
            if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
                if chars:
                    chars.pop()
                    stdscr.move(0, input_x + min(len(chars), max_chars - 1))
                    stdscr.delch()
                continue
            if isinstance(key, str) and key >= " " and len(chars) < max_chars:
                chars.append(key)
                stdscr.addstr(0, input_x + len(chars) - 1, "*")
                stdscr.refresh()
    finally:
        curses.noecho()
        with contextlib.suppress(curses.error):
            curses.curs_set(0)


def show_curses_message(stdscr: Any, message: str) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    add_line(stdscr, 0, 0, message, width, 0)
    add_line(stdscr, min(2, height - 1), 0, "Press any key to continue.", width, 0)
    stdscr.refresh()
    stdscr.getch()


def add_popup(stdscr: Any, y: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0:
        return
    height, _ = stdscr.getmaxyx()
    if y >= height or width <= 1:
        return
    add_line(stdscr, y, 0, "", width)
    popup_text = f" {text} "
    max_chars = max(1, width - 1)
    if len(popup_text) > max_chars:
        popup_text = popup_text[:max_chars]
    x = max(0, (width - len(popup_text)) // 2)
    stdscr.addnstr(y, x, popup_text, max_chars, attr)


def add_line(stdscr: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0:
        return
    height, _ = stdscr.getmaxyx()
    if y >= height:
        return
    if width <= x:
        return
    max_chars = max(0, width - x - 1)
    if max_chars == 0:
        return
    stdscr.move(y, x)
    stdscr.clrtoeol()
    if text:
        stdscr.addnstr(y, x, text, max_chars, attr)


def cmd_status(switcher: "Switcher", args: argparse.Namespace) -> int:
    del args
    status = switcher.status()
    print(f"CODEX_HOME: {status['codex_home']}")
    print(f"switcher store: {status['store']}")
    print(f"active context: {status['active_context']}")
    print(f"model: {status['model']}")
    print(f"provider: {status['provider']}")
    print(f"provider auth: {status['provider_auth']}")
    print(f"auth.json: {status['auth_json']}")
    print(f"five-hour: {status['five_hour_detail']}")
    print(f"weekly: {status['weekly_detail']}")
    print(f"limits: {status['limits_cache']}")
    return 0


def cmd_drop_auth(switcher: "Switcher", args: argparse.Namespace) -> int:
    switcher.drop_auth(args.name)
    print(f"removed auth.json from context {args.name!r}")
    return 0


class Switcher:
    def __init__(self, store: Path, codex_home: Path, codex_bin: str) -> None:
        self.store = store
        self.codex_home = codex_home
        self.codex_bin = codex_bin
        self.contexts_dir = self.store / "contexts"
        self.backups_dir = self.store / "backups"
        self.homes_dir = self.store / "homes"
        self.active_file = self.store / "active.json"
        ensure_dir(self.store, mode=0o700)
        ensure_dir(self.contexts_dir, mode=0o700)
        ensure_dir(self.backups_dir, mode=0o700)
        ensure_dir(self.homes_dir, mode=0o700)

    @contextlib.contextmanager
    def lock(self) -> Any:
        ensure_dir(self.store, mode=0o700)
        lock_path = self.store / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def capture(self, name: str, overwrite: bool = False) -> None:
        context_dir = self.context_dir(name)
        if context_dir.exists() and not overwrite:
            raise SwitcherError(f"context {name!r} already exists; use --overwrite")
        source_config = self.codex_home / "config.toml"
        if not source_config.exists():
            raise SwitcherError(f"{source_config} does not exist")

        if context_dir.exists():
            shutil.rmtree(context_dir)
        ensure_dir(context_dir, mode=0o700)
        copy_secret_file(source_config, context_dir / "config.toml")
        source_auth = self.codex_home / "auth.json"
        if source_auth.exists():
            copy_secret_file(source_auth, context_dir / "auth.json")
        self.write_metadata(context_dir, name, "capture")

    def use(self, name: str, keep_auth: bool = False) -> None:
        context_dir = self.require_context(name)
        context_config = context_dir / "config.toml"
        if not context_config.exists():
            raise SwitcherError(f"context {name!r} has no config.toml")

        ensure_dir(self.codex_home, mode=0o700)
        self.backup_active_files(reason=f"use-{name}")

        config = read_toml(context_config)
        preserve_active_model_settings(config, self.codex_home / "config.toml")
        write_text_secret(self.codex_home / "config.toml", dumps_toml(config))
        context_auth = context_dir / "auth.json"
        target_auth = self.codex_home / "auth.json"
        if context_auth.exists():
            copy_secret_file(context_auth, target_auth)
        elif target_auth.exists() and not keep_auth:
            target_auth.unlink()

        write_json_secret(
            self.active_file,
            {
                "name": name,
                "codex_home": str(self.codex_home),
                "switched_at": utc_now(),
            },
        )

    def create_provider_context(
        self,
        *,
        name: str,
        provider_id: str,
        model: str,
        provider_name: str | None,
        base_url: str | None,
        wire_api: str | None,
        supports_websockets: bool | None = None,
        env_key: str | None,
        api_key: str | None,
        requires_openai_auth: bool,
        reasoning_effort: str | None,
        overwrite: bool,
    ) -> None:
        validate_context_name(name)
        validate_provider_id(provider_id)
        auth_modes = [bool(env_key), bool(api_key), bool(requires_openai_auth)]
        if sum(auth_modes) > 1:
            raise SwitcherError(
                "choose only one provider auth mode: --env-key, --api-key, or --requires-openai-auth"
            )
        context_dir = self.context_dir(name)
        if context_dir.exists() and not overwrite:
            raise SwitcherError(f"context {name!r} already exists; use --overwrite")
        config = self.load_template_config(existing_context=context_dir if context_dir.exists() else None)
        providers = config.get("model_providers")
        existing_provider = providers.get(provider_id) if isinstance(providers, dict) else None
        existing_provider_config = existing_provider if isinstance(existing_provider, dict) else None
        if provider_id not in BUILTIN_PROVIDERS and not base_url and existing_provider_config is None:
            raise SwitcherError("custom providers require --base-url unless the provider already exists in config.toml")

        config["model"] = model
        config["model_provider"] = provider_id
        if reasoning_effort:
            config["model_reasoning_effort"] = reasoning_effort
        if provider_id != "openai":
            config.pop("openai_base_url", None)

        if provider_id == "openai":
            if base_url:
                config["openai_base_url"] = base_url
        elif provider_id in {"ollama", "lmstudio"}:
            if base_url:
                providers = ensure_table(config, "model_providers")
                provider_config: dict[str, Any] = {}
                provider_config["base_url"] = base_url
                provider_config["wire_api"] = wire_api or "responses"
                provider_config["name"] = provider_name or provider_id
                providers[provider_id] = provider_config
        else:
            providers = ensure_table(config, "model_providers")
            provider_config = dict(existing_provider_config or {})
            provider_config["name"] = provider_name or provider_id
            if base_url:
                provider_config["base_url"] = base_url
            if not provider_config.get("base_url"):
                raise SwitcherError("custom providers require --base-url")
            if wire_api or not provider_config.get("wire_api"):
                provider_config["wire_api"] = wire_api or "responses"
            provider_config["supports_websockets"] = bool(supports_websockets)
            if env_key or api_key or requires_openai_auth:
                for auth_key in ("env_key", "experimental_bearer_token", "requires_openai_auth"):
                    provider_config.pop(auth_key, None)
            if env_key:
                provider_config["env_key"] = env_key
            elif api_key:
                provider_config["experimental_bearer_token"] = api_key
            elif requires_openai_auth:
                provider_config["requires_openai_auth"] = True
            providers[provider_id] = provider_config

        if context_dir.exists():
            shutil.rmtree(context_dir)
        ensure_dir(context_dir, mode=0o700)
        write_text_secret(context_dir / "config.toml", dumps_toml(config))
        self.write_metadata(context_dir, name, "provider")

    def login(
        self,
        *,
        name: str,
        provider_id: str,
        model: str,
        base_url: str | None,
        device_auth: bool,
        with_api_key: bool,
        with_access_token: bool,
        activate: bool,
        secret_input: str | None = None,
    ) -> None:
        validate_context_name(name)
        validate_provider_id(provider_id)
        base_url = base_url.strip() if base_url else None

        context_dir = self.context_dir(name)
        config = self.load_template_config(existing_context=context_dir if context_dir.exists() else None)
        config["model_provider"] = provider_id
        config["model"] = model
        config["cli_auth_credentials_store"] = "file"
        if base_url:
            if provider_id == "openai":
                config["openai_base_url"] = base_url
            else:
                providers = ensure_table(config, "model_providers")
                provider = providers.get(provider_id)
                if not isinstance(provider, dict):
                    raise SwitcherError(
                        f"provider {provider_id!r} is not defined in the template config; create it first"
                    )
                provider["base_url"] = base_url

        if provider_id not in BUILTIN_PROVIDERS:
            providers = config.get("model_providers", {})
            if provider_id not in providers:
                raise SwitcherError(
                    f"provider {provider_id!r} is not defined in the template config; create it first"
                )

        ensure_dir(context_dir, mode=0o700)
        write_text_secret(context_dir / "config.toml", dumps_toml(config))

        home = self.prepare_isolated_home(name)
        login_args = ["login"]
        if device_auth:
            login_args.append("--device-auth")
        if with_api_key:
            login_args.append("--with-api-key")
        if with_access_token:
            login_args.append("--with-access-token")
        login_cmd = command_for_subprocess(self.codex_bin, login_args)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        # Keep stdin connected so `--with-api-key` and `--with-access-token`
        # can receive piped secrets exactly as `codex login` expects.
        if secret_input is None:
            result = subprocess.run(login_cmd, env=env, stdin=sys.stdin)
        else:
            result = subprocess.run(login_cmd, env=env, input=secret_input, text=True)
        if result.returncode != 0:
            raise SwitcherError(f"codex login failed with exit code {result.returncode}")

        isolated_auth = home / "auth.json"
        if not isolated_auth.exists():
            raise SwitcherError(
                "codex login succeeded but did not write auth.json; check credential storage settings"
            )
        copy_secret_file(isolated_auth, context_dir / "auth.json")
        self.write_metadata(context_dir, name, "login")
        if activate:
            self.use(name)

    def run(self, name: str, cmd: list[str]) -> int:
        context_dir = self.require_context(name)
        home = self.prepare_isolated_home(name)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        if cmd and cmd[0] == "codex":
            cmd = command_for_subprocess(self.codex_bin, cmd[1:])
        else:
            cmd = command_for_subprocess(cmd[0], cmd[1:])
        result = subprocess.run(cmd, env=env)

        isolated_auth = home / "auth.json"
        context_auth = context_dir / "auth.json"
        if isolated_auth.exists():
            copy_secret_file(isolated_auth, context_auth)
        elif context_auth.exists():
            context_auth.unlink()
        return result.returncode

    def list_contexts(self, refresh_limits: bool = False) -> list[dict[str, str]]:
        active = self.read_active_context()
        rows = []
        for path in sorted(self.contexts_dir.iterdir() if self.contexts_dir.exists() else []):
            if not path.is_dir():
                continue
            config = read_toml(path / "config.toml") if (path / "config.toml").exists() else {}
            row = {
                "active": "*" if active == path.name else "",
                "name": path.name,
                "model": str(config.get("model", "")),
                "provider": str(config.get("model_provider", "openai")),
                "auth": "yes" if (path / "auth.json").exists() else "no",
                "provider_auth": provider_auth_summary(config),
            }
            row.update(self.context_limit_summary(path, config, refresh=refresh_limits))
            rows.append(row)
        return rows

    def status(self) -> dict[str, str]:
        config_path = self.codex_home / "config.toml"
        config = read_toml(config_path) if config_path.exists() else {}
        auth_path = self.codex_home / "auth.json"
        status = {
            "codex_home": str(self.codex_home),
            "store": str(self.store),
            "active_context": self.read_active_context() or "(unknown)",
            "model": str(config.get("model", "")),
            "provider": str(config.get("model_provider", "openai")),
            "provider_auth": provider_auth_summary(config),
            "auth_json": "present" if auth_path.exists() else "missing",
        }
        status.update(self.active_limit_summary(config, refresh=True))
        return status

    def context_limit_summary(self, context_dir: Path, config: dict[str, Any], refresh: bool) -> dict[str, str]:
        if not uses_codex_rate_limits(config):
            return empty_limit_summary("n/a", "provider does not use Codex/OpenAI auth")
        if not (context_dir / "auth.json").exists():
            return empty_limit_summary("not signed in", "no auth.json saved for this context")

        cache_path = context_dir / "rate_limits.json"
        if refresh:
            try:
                snapshot = self.fetch_rate_limits_for_context(context_dir.name)
                write_json_secret(cache_path, {"fetched_at": utc_now(), "snapshot": snapshot})
                return summarize_rate_limits(snapshot, source="fresh")
            except SwitcherError as exc:
                return empty_limit_summary("unavailable", f"fresh fetch failed: {exc}")

        cached = read_rate_limit_cache(cache_path)
        if cached is not None:
            return summarize_rate_limits(cached["snapshot"], source="cached")
        return empty_limit_summary("?", "not fetched yet")

    def active_limit_summary(self, config: dict[str, Any], refresh: bool) -> dict[str, str]:
        if not uses_codex_rate_limits(config):
            return empty_limit_summary("n/a", "provider does not use Codex/OpenAI auth")
        if not (self.codex_home / "auth.json").exists():
            return empty_limit_summary("not signed in", "no auth.json in active CODEX_HOME")
        cache_path = self.store / "active-rate-limits.json"
        if refresh:
            try:
                snapshot = fetch_rate_limits_from_codex_home(
                    codex_bin=self.codex_bin,
                    codex_home=self.codex_home,
                    timeout_seconds=12.0,
                )
                write_json_secret(cache_path, {"fetched_at": utc_now(), "snapshot": snapshot})
                return summarize_rate_limits(snapshot, source="fresh")
            except SwitcherError as exc:
                return empty_limit_summary("unavailable", f"fresh fetch failed: {exc}")
        return empty_limit_summary("?", "not fetched")

    def fetch_rate_limits_for_context(self, name: str) -> dict[str, Any]:
        home = self.prepare_isolated_home(name)
        return fetch_rate_limits_from_codex_home(
            codex_bin=self.codex_bin,
            codex_home=home,
            timeout_seconds=12.0,
        )

    def drop_auth(self, name: str) -> None:
        context_dir = self.require_context(name)
        auth = context_dir / "auth.json"
        if auth.exists():
            auth.unlink()

    def delete_context(self, name: str) -> None:
        context_dir = self.require_context(name)
        shutil.rmtree(context_dir)

        home = self.homes_dir / name
        if home.exists():
            shutil.rmtree(home)

        if self.read_active_context() == name and self.active_file.exists():
            self.active_file.unlink()

    def prepare_isolated_home(self, name: str) -> Path:
        context_dir = self.require_context(name)
        home = self.homes_dir / name
        ensure_dir(home, mode=0o700)
        copy_secret_file(context_dir / "config.toml", home / "config.toml")
        context_auth = context_dir / "auth.json"
        home_auth = home / "auth.json"
        if context_auth.exists():
            copy_secret_file(context_auth, home_auth)
        elif home_auth.exists():
            home_auth.unlink()
        return home

    def load_template_config(self, existing_context: Path | None = None) -> dict[str, Any]:
        candidate_paths = []
        if existing_context is not None:
            candidate_paths.append(existing_context / "config.toml")
        candidate_paths.append(self.codex_home / "config.toml")
        for path in candidate_paths:
            if path.exists():
                return read_toml(path)
        return {"model_provider": "openai", "model": "gpt-5.5"}

    def require_context(self, name: str) -> Path:
        context_dir = self.context_dir(name)
        if not context_dir.is_dir():
            raise SwitcherError(f"context {name!r} does not exist")
        return context_dir

    def context_dir(self, name: str) -> Path:
        validate_context_name(name)
        return self.contexts_dir / name

    def backup_active_files(self, reason: str) -> None:
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_dir = unique_path(self.backups_dir / f"{timestamp}-{sanitize_filename(reason)}")
        ensure_dir(backup_dir, mode=0o700)
        for file_name in ("config.toml", "auth.json"):
            source = self.codex_home / file_name
            if source.exists():
                copy_secret_file(source, backup_dir / file_name)
        write_json_secret(
            backup_dir / "metadata.json",
            {"created_at": utc_now(), "codex_home": str(self.codex_home), "reason": reason},
        )

    def write_metadata(self, context_dir: Path, name: str, source: str) -> None:
        config = read_toml(context_dir / "config.toml") if (context_dir / "config.toml").exists() else {}
        write_json_secret(
            context_dir / "metadata.json",
            {
                "name": name,
                "source": source,
                "updated_at": utc_now(),
                "model": config.get("model"),
                "model_provider": config.get("model_provider", "openai"),
                "auth_json": (context_dir / "auth.json").exists(),
            },
        )

    def read_active_context(self) -> str | None:
        if not self.active_file.exists():
            return None
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("codex_home") != str(self.codex_home):
            return None
        name = data.get("name")
        return name if isinstance(name, str) else None

    def app_server_may_be_running(self) -> bool:
        if os.name == "nt":
            return bool(find_windows_process_ids("Codex"))
        pid_file = self.codex_home / "app-server-daemon" / "app-server.pid"
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return Path(f"/proc/{pid}").exists()

    def restart_codex_app(self) -> None:
        if os.name != "nt":
            if self.app_server_may_be_running():
                print("note: automatic app restart is only implemented on Windows.")
            return
        for pid in find_windows_process_ids("Codex"):
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        if launch_windows_codex_app():
            print("restarted Codex app.")
        else:
            print("note: Codex app was stopped but could not be located for relaunch.")


def provider_auth_summary(config: dict[str, Any]) -> str:
    provider_id = str(config.get("model_provider", "openai"))
    if provider_id == "openai":
        return "codex-auth"
    providers = config.get("model_providers")
    provider = providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    if not isinstance(provider, dict):
        return "unknown"
    if provider.get("requires_openai_auth"):
        return "codex-auth"
    if provider.get("env_key"):
        return f"env:{provider['env_key']}"
    if provider.get("experimental_bearer_token"):
        return "bearer-token"
    if provider.get("auth"):
        return "auth-command"
    return "none"


def uses_codex_rate_limits(config: dict[str, Any]) -> bool:
    provider_id = str(config.get("model_provider", "openai"))
    if provider_id == "openai":
        return True
    providers = config.get("model_providers")
    provider = providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    return isinstance(provider, dict) and bool(provider.get("requires_openai_auth"))


def find_windows_process_ids(image_name: str) -> list[int]:
    if os.name != "nt":
        return []
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        f"Get-Process -Name {json.dumps(image_name)} -ErrorAction SilentlyContinue | ForEach-Object Id",
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    ids: list[int] = []
    for line in result.stdout.splitlines():
        with contextlib.suppress(ValueError):
            ids.append(int(line.strip()))
    return ids


def launch_windows_codex_app() -> bool:
    candidates: list[list[str]] = []
    package_family = get_windows_codex_package_family()
    if package_family:
        candidates.append(["explorer.exe", f"shell:AppsFolder\\{package_family}!App"])
    candidates.append(["explorer.exe", "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App"])
    local_app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Codex" / "Codex.exe"
    if local_app.exists():
        candidates.append([str(local_app)])
    for candidate in candidates:
        with contextlib.suppress(OSError):
            subprocess.Popen(candidate, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def get_windows_codex_package_family() -> str | None:
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-Command",
        "(Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue).PackageFamilyName",
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def command_for_subprocess(executable: str, args: list[str]) -> list[str]:
    if os.name != "nt":
        return [executable, *args]
    path = Path(executable)
    if path.exists() and path.suffix.lower() not in {".exe", ".cmd", ".bat", ".com"}:
        if path.suffix.lower() == ".ps1":
            return [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(path),
                *args,
            ]
        return [sys.executable, str(path), *args]
    return [executable, *args]


def preserve_active_model_settings(target_config: dict[str, Any], active_config_path: Path) -> None:
    if not active_config_path.exists():
        return
    active_config = read_toml(active_config_path)
    model_keys = {
        key
        for key in set(target_config) | set(active_config)
        if is_model_setting_key(key)
    }
    for key in model_keys:
        if key in active_config:
            target_config[key] = active_config[key]
        else:
            target_config.pop(key, None)


def is_model_setting_key(key: str) -> bool:
    return key == "model" or (key.startswith("model_") and key != "model_provider")


def fetch_rate_limits_from_codex_home(
    *,
    codex_bin: str,
    codex_home: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    stdout_queue: Queue[str | None] = Queue()
    stderr_queue: Queue[str] = Queue()
    try:
        proc = subprocess.Popen(
            command_for_subprocess(codex_bin, ["app-server", "--stdio"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise SwitcherError(f"failed to start Codex app server: {exc}") from None
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codexswitcher", "version": "0"},
                "protocolVersion": "2",
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
    ]
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                stdout_queue.put(line)
        finally:
            stdout_queue.put(None)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_queue.put(line)

    threading.Thread(target=read_stdout, name="codexswitcher-rate-stdout", daemon=True).start()
    threading.Thread(target=read_stderr, name="codexswitcher-rate-stderr", daemon=True).start()
    try:
        assert proc.stdin is not None
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            while len(stderr_lines) < 5:
                try:
                    stderr_lines.append(stderr_queue.get_nowait().strip())
                except Empty:
                    break
            remaining = max(0.0, deadline - time.monotonic())
            try:
                line = stdout_queue.get(timeout=min(0.2, remaining))
            except Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != 2:
                continue
            if "error" in payload:
                error = payload["error"]
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("code") or error)
                else:
                    message = str(error)
                raise SwitcherError(message)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise SwitcherError("rate-limit response did not include an object result")
            snapshot = choose_rate_limit_snapshot(result)
            if snapshot is None:
                raise SwitcherError("rate-limit response did not include a Codex limit snapshot")
            return snapshot
        detail = "; ".join(line for line in stderr_lines if line)
        raise SwitcherError(f"timed out reading fresh rate limits{': ' + detail if detail else ''}")
    finally:
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        if proc.poll() is None:
            proc.kill()


def choose_rate_limit_snapshot(response: dict[str, Any]) -> dict[str, Any] | None:
    by_id = response.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        preferred = by_id.get("codex")
        if isinstance(preferred, dict):
            return preferred
        for value in by_id.values():
            if isinstance(value, dict):
                return value
    snapshot = response.get("rateLimits")
    return snapshot if isinstance(snapshot, dict) else None


def read_rate_limit_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    snapshot = data.get("snapshot")
    return data if isinstance(snapshot, dict) else None


def summarize_rate_limits(snapshot: dict[str, Any], source: str) -> dict[str, str]:
    del source
    primary = window_from_snapshot(snapshot, "primary")
    secondary = window_from_snapshot(snapshot, "secondary")
    plan = value_by_keys(snapshot, "planType", "plan_type") or "unknown"
    reached = value_by_keys(snapshot, "rateLimitReachedType", "rate_limit_reached_type")
    cache = f"plan={plan}"
    if reached:
        cache += f"; reached={reached}"
    return {
        "five_hour": short_window(primary),
        "weekly": short_window(secondary),
        "five_hour_detail": detail_window(primary, "five-hour"),
        "weekly_detail": detail_window(secondary, "weekly"),
        "limits_cache": cache,
    }


def empty_limit_summary(value: str, detail: str) -> dict[str, str]:
    return {
        "five_hour": value,
        "weekly": value,
        "five_hour_detail": detail,
        "weekly_detail": detail,
        "limits_cache": detail,
    }


def window_from_snapshot(snapshot: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = snapshot.get(key)
    return value if isinstance(value, dict) else None


def short_window(window: dict[str, Any] | None) -> str:
    if not window:
        return "?"
    percent = value_by_keys(window, "usedPercent", "used_percent")
    if isinstance(percent, (int, float)):
        return f"{max(0.0, min(100.0, 100.0 - float(percent))):.0f}%"
    return "?"


def detail_window(window: dict[str, Any] | None, label: str) -> str:
    if not window:
        return f"{label}: unavailable"
    percent = value_by_keys(window, "usedPercent", "used_percent")
    resets_at = value_by_keys(window, "resetsAt", "resets_at")
    parts = [label]
    if isinstance(percent, (int, float)):
        remaining = max(0.0, min(100.0, 100.0 - float(percent)))
        parts.append(f"{remaining:.0f}% remaining")
    else:
        parts.append("usage unknown")
    if isinstance(resets_at, int):
        parts.append(f"resets {format_epoch(resets_at)}")
    return "; ".join(parts)


def value_by_keys(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def format_epoch(value: int) -> str:
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(value)


def read_toml(path: Path) -> dict[str, Any]:
    try:
        if tomllib is not None:
            with path.open("rb") as file:
                data = tomllib.load(file)
        else:
            data = parse_basic_toml(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SwitcherError(f"{path} does not exist") from None
    except TomlParseError as exc:
        raise SwitcherError(f"failed to parse {path}: {exc}") from None
    except Exception as exc:
        if exc.__class__.__name__ != "TOMLDecodeError":
            raise
        raise SwitcherError(f"failed to parse {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SwitcherError(f"{path} did not parse to a TOML table")
    return data


class TomlParseError(ValueError):
    pass


def parse_basic_toml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current = root
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table_name = line[1:-1].strip()
            if not table_name:
                raise TomlParseError(f"empty table name on line {line_number}")
            current = root
            for part in split_toml_dotted_key(table_name):
                value = current.setdefault(part, {})
                if not isinstance(value, dict):
                    raise TomlParseError(f"table {table_name!r} conflicts with a scalar on line {line_number}")
                current = value
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise TomlParseError(f"expected key = value on line {line_number}")
        current[parse_toml_key(key.strip())] = parse_basic_toml_value(value.strip())
    return root


def strip_toml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def split_toml_dotted_key(key: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in key:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == ".":
            parts.append(parse_toml_key("".join(current).strip()))
            current = []
        else:
            current.append(char)
    if quote:
        raise TomlParseError(f"unterminated quoted key {key!r}")
    parts.append(parse_toml_key("".join(current).strip()))
    return parts


def parse_toml_key(key: str) -> str:
    if not key:
        raise TomlParseError("empty key")
    if key[0] in {"'", '"'}:
        try:
            return json.loads(key) if key[0] == '"' else key[1:-1]
        except json.JSONDecodeError as exc:
            raise TomlParseError(str(exc)) from exc
    return key


def parse_basic_toml_value(value: str) -> Any:
    if value.startswith('"') or value.startswith("'"):
        if value.startswith("'"):
            if not value.endswith("'"):
                raise TomlParseError("unterminated literal string")
            return value[1:-1]
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TomlParseError(str(exc)) from exc
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_basic_toml_value(part.strip()) for part in split_toml_inline_items(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        result: dict[str, Any] = {}
        if not inner:
            return result
        for part in split_toml_inline_items(inner):
            key, separator, item_value = part.partition("=")
            if not separator:
                raise TomlParseError(f"invalid inline table item {part!r}")
            result[parse_toml_key(key.strip())] = parse_basic_toml_value(item_value.strip())
        return result
    with contextlib.suppress(ValueError):
        return int(value)
    with contextlib.suppress(ValueError):
        return float(value)
    raise TomlParseError(f"unsupported TOML value {value!r}")


def split_toml_inline_items(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    for char in text:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote:
        raise TomlParseError("unterminated quoted inline value")
    if current:
        parts.append("".join(current))
    return parts


def dumps_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    write_toml_table(lines, data, ())
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def write_toml_table(lines: list[str], table: dict[str, Any], prefix: tuple[str, ...]) -> None:
    for key, value in table.items():
        if not isinstance(value, dict):
            lines.append(f"{toml_key(key)} = {toml_value(value)}")

    for key, value in table.items():
        if not isinstance(value, dict):
            continue
        full_prefix = prefix + (str(key),)
        lines.append("")
        lines.append(f"[{'.'.join(toml_key(part) for part in full_prefix)}]")
        write_toml_table(lines, value, full_prefix)


def toml_key(key: str) -> str:
    key = str(key)
    if BARE_TOML_KEY_RE.match(key):
        return key
    return json.dumps(key)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{toml_key(str(k))} = {toml_value(v)}" for k, v in value.items())
        return "{ " + items + " }"
    if value is None:
        raise SwitcherError("TOML does not support null values")
    raise SwitcherError(f"cannot write TOML value of type {type(value).__name__}")


def ensure_table(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.setdefault(key, {})
    if not isinstance(value, dict):
        raise SwitcherError(f"config key {key!r} is not a table")
    return value


def validate_context_name(name: str) -> None:
    if not CONTEXT_NAME_RE.match(name):
        raise SwitcherError("context names may only contain letters, numbers, dot, underscore, and hyphen")


def validate_provider_id(provider_id: str) -> None:
    if not BARE_TOML_KEY_RE.match(provider_id):
        raise SwitcherError("provider ids may only contain letters, numbers, underscore, and hyphen")


def expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def ensure_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(mode)


def copy_secret_file(source: Path, target: Path) -> None:
    ensure_dir(target.parent, mode=0o700)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file, source.open("rb") as source_file:
            shutil.copyfileobj(source_file, tmp_file)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def write_text_secret(path: Path, text: str) -> None:
    write_bytes_secret(path, text.encode("utf-8"), mode=0o600)


def write_json_secret(path: Path, value: dict[str, Any]) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    write_text_secret(path, text)


def write_bytes_secret(path: Path, data: bytes, mode: int) -> None:
    ensure_dir(path.parent, mode=0o700)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            tmp_file.write(data)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "backup"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for suffix in range(1, 1000):
        candidate = path.with_name(f"{path.name}-{suffix}")
        if not candidate.exists():
            return candidate
    raise SwitcherError(f"could not allocate a unique backup path under {path.parent}")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
