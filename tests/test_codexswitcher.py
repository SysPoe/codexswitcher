from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codexswitcher

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codexswitcher.py"


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class CodexSwitcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex-home"
        self.store = self.root / "store"
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                approvals_reviewer = "guardian_subagent"
                model = "gpt-5.5"
                model_provider = "customapi"
                model_reasoning_effort = "xhigh"

                [model_providers.customapi]
                name = "Custom API"
                base_url = "https://api.example.test/v1"
                experimental_bearer_token = "secret-token"
                wire_api = "responses"

                [projects."/tmp/example"]
                trust_level = "trusted"

                [plugins."github@openai-curated"]
                enabled = true

                [tui]
                status_line = ["model-with-reasoning", "current-dir"]
                status_line_use_colors = true
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (self.codex_home / "auth.json").write_text('{"kind":"old"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_switcher(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            *args,
        ]
        return subprocess.run(cmd, input=input_text, text=True, capture_output=True, check=False)

    def test_capture_and_use_round_trip(self) -> None:
        result = self.run_switcher("capture", "custom-context")
        self.assertEqual(result.returncode, 0, result.stderr)
        captured_config = self.store / "contexts" / "custom-context" / "config.toml"
        self.assertTrue(captured_config.exists())
        self.assertTrue((self.store / "contexts" / "custom-context" / "auth.json").exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(captured_config.stat().st_mode), 0o600)

        self.run_switcher(
            "provider",
            "openai-work",
            "--provider-id",
            "openai",
            "--model",
            "gpt-context",
        )
        (self.codex_home / "auth.json").write_text('{"kind":"should-be-removed"}\n', encoding="utf-8")
        result = self.run_switcher("use", "openai-work")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "openai"', config)
        self.assertIn('model = "gpt-5.5"', config)
        self.assertNotIn('model = "gpt-context"', config)
        self.assertIn('model_reasoning_effort = "xhigh"', config)
        self.assertFalse((self.codex_home / "auth.json").exists())

        result = self.run_switcher("use", "custom-context")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertEqual(json.loads((self.codex_home / "auth.json").read_text()), {"kind": "old"})

    def test_use_preserves_active_reasoning_effort(self) -> None:
        switcher = codexswitcher.Switcher(self.store, self.codex_home, "codex")
        switcher.create_provider_context(
            name="openai-xhigh",
            provider_id="openai",
            model="gpt-context",
            provider_name=None,
            base_url=None,
            wire_api=None,
            supports_websockets=None,
            env_key=None,
            api_key=None,
            requires_openai_auth=False,
            reasoning_effort="xhigh",
            overwrite=False,
        )
        (self.codex_home / "config.toml").write_text(
            textwrap.dedent(
                """
                model = "gpt-5.5"
                model_provider = "customapi"
                model_reasoning_effort = "medium"
                model_verbosity = "low"

                [model_providers.customapi]
                name = "Custom API"
                base_url = "https://api.example.test/v1"
                experimental_bearer_token = "secret-token"
                wire_api = "responses"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        switcher.use("openai-xhigh")

        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "openai"', config)
        self.assertIn('model = "gpt-5.5"', config)
        self.assertIn('model_reasoning_effort = "medium"', config)
        self.assertIn('model_verbosity = "low"', config)
        self.assertNotIn('model = "gpt-context"', config)
        self.assertNotIn('model_reasoning_effort = "xhigh"', config)

    @unittest.skipIf(os.name == "nt", "curses selector flow is not used on Windows")
    def test_tui_reopens_after_switch_and_delete_actions(self) -> None:
        switcher = codexswitcher.Switcher(self.store, self.codex_home, "codex")
        switcher.capture("custom-context")
        switcher.create_provider_context(
            name="openai-work",
            provider_id="openai",
            model="gpt-5.5",
            provider_name=None,
            base_url=None,
            wire_api=None,
            supports_websockets=None,
            env_key=None,
            api_key=None,
            requires_openai_auth=False,
            reasoning_effort=None,
            overwrite=False,
        )
        actions = iter(
            [
                {"action": "use", "name": "openai-work"},
                {"action": "delete", "name": "custom-context"},
                None,
            ]
        )
        calls: list[tuple[list[str], str, list[str]]] = []

        def fake_select_context_tui(
            rows: list[dict[str, str]],
            selected_switcher: codexswitcher.Switcher,
            initial_message: str = "",
        ) -> dict[str, str] | None:
            self.assertIs(selected_switcher, switcher)
            calls.append(
                (
                    [row["name"] for row in rows],
                    initial_message,
                    [row["name"] for row in rows if row["active"] == "*"],
                )
            )
            return next(actions)

        with (
            mock.patch.object(codexswitcher.sys, "stdin", TtyBuffer()),
            mock.patch.object(codexswitcher.sys, "stdout", TtyBuffer()),
            mock.patch.object(codexswitcher, "select_context_tui", fake_select_context_tui),
        ):
            result = codexswitcher.cmd_tui(switcher, SimpleNamespace())

        self.assertEqual(result, 130)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], "")
        self.assertIn("openai-work", calls[1][2])
        self.assertIn("activated context 'openai-work'", calls[1][1])
        self.assertNotIn("custom-context", calls[2][0])
        self.assertIn("deleted context 'custom-context'", calls[2][1])
        self.assertFalse((self.store / "contexts" / "custom-context").exists())

    def test_provider_context_preserves_unrelated_config(self) -> None:
        result = self.run_switcher(
            "provider",
            "custom-env",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--env-key",
            "CUSTOM_API_KEY",
            "--overwrite",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config_path = self.store / "contexts" / "custom-env" / "config.toml"
        config = config_path.read_text(encoding="utf-8")
        self.assertIn('[projects."/tmp/example"]', config)
        self.assertIn('[plugins."github@openai-curated"]', config)
        self.assertIn('base_url = "https://api.example.test/v1"', config)
        self.assertIn("supports_websockets = false", config)
        self.assertIn('env_key = "CUSTOM_API_KEY"', config)
        self.assertNotIn("secret-token", config)

    def test_existing_provider_accepts_api_key_without_reentering_base_url(self) -> None:
        result = self.run_switcher(
            "provider",
            "existing-provider-key",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--api-key",
            "new-secret-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "existing-provider-key" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn('base_url = "https://api.example.test/v1"', config)
        self.assertIn('wire_api = "responses"', config)
        self.assertIn("supports_websockets = false", config)
        self.assertIn('experimental_bearer_token = "new-secret-token"', config)

    def test_existing_provider_can_be_saved_without_reentering_secret(self) -> None:
        result = self.run_switcher(
            "provider",
            "existing-provider-current-key",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "existing-provider-current-key" / "config.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn('base_url = "https://api.example.test/v1"', config)
        self.assertIn("supports_websockets = false", config)
        self.assertIn('experimental_bearer_token = "secret-token"', config)

    def test_unknown_custom_provider_requires_base_url(self) -> None:
        result = self.run_switcher(
            "provider",
            "missing-base",
            "--provider-id",
            "unknownapi",
            "--model",
            "gpt-5.5",
            "--api-key",
            "secret-token",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("custom providers require --base-url", result.stderr)

    def test_custom_provider_accepts_manual_base_url_and_api_key(self) -> None:
        result = self.run_switcher(
            "provider",
            "manual-provider",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--base-url",
            "https://api.example.test/v1",
            "--api-key",
            "secret-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "manual-provider" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn('base_url = "https://api.example.test/v1"', config)
        self.assertIn("supports_websockets = false", config)
        self.assertIn('experimental_bearer_token = "secret-token"', config)

    def test_new_custom_provider_defaults_provider_name_to_provider_id(self) -> None:
        result = self.run_switcher(
            "provider",
            "manual-provider-name",
            "--provider-id",
            "newcustom",
            "--model",
            "gpt-5.5",
            "--base-url",
            "https://api.example.test/v1",
            "--api-key",
            "secret-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "manual-provider-name" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('[model_providers.newcustom]', config)
        self.assertIn('name = "newcustom"', config)

    def test_custom_provider_removes_stale_openai_base_url(self) -> None:
        config_path = self.codex_home / "config.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'model_reasoning_effort = "xhigh"',
                'model_reasoning_effort = "xhigh"\nopenai_base_url = "https://api.example.test/v1"',
            ),
            encoding="utf-8",
        )
        result = self.run_switcher(
            "provider",
            "custom-provider",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "custom-provider" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn('base_url = "https://api.example.test/v1"', config)
        self.assertIn("supports_websockets = false", config)
        self.assertNotIn("openai_base_url", config)

    def test_custom_provider_can_opt_into_responses_websockets(self) -> None:
        result = self.run_switcher(
            "provider",
            "manual-provider-ws",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--base-url",
            "https://api.example.test/v1",
            "--api-key",
            "secret-token",
            "--supports-websockets",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "manual-provider-ws" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn("supports_websockets = true", config)

    def test_custom_provider_defaults_responses_websockets_off_even_if_template_enabled(self) -> None:
        config_path = self.codex_home / "config.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'wire_api = "responses"',
                'wire_api = "responses"\nsupports_websockets = true',
            ),
            encoding="utf-8",
        )
        result = self.run_switcher(
            "provider",
            "manual-provider-default-no-ws",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--api-key",
            "secret-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "manual-provider-default-no-ws" / "config.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn("supports_websockets = false", config)

    def test_custom_provider_can_force_responses_websockets_off(self) -> None:
        config_path = self.codex_home / "config.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'wire_api = "responses"',
                'wire_api = "responses"\nsupports_websockets = true',
            ),
            encoding="utf-8",
        )
        result = self.run_switcher(
            "provider",
            "manual-provider-no-ws",
            "--provider-id",
            "customapi",
            "--model",
            "gpt-5.5",
            "--api-key",
            "secret-token",
            "--no-supports-websockets",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "manual-provider-no-ws" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_provider = "customapi"', config)
        self.assertIn("supports_websockets = false", config)

    def test_login_uses_isolated_home_and_stores_auth(self) -> None:
        fake_codex = self.root / "fake-codex"
        fake_codex.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys

                assert sys.argv[:2] == [sys.argv[0], "login"]
                home = pathlib.Path(os.environ["CODEX_HOME"])
                config = (home / "config.toml").read_text()
                assert 'cli_auth_credentials_store = "file"' in config
                (home / "auth.json").write_text(json.dumps({"kind": "new-login"}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "login",
            "personal",
            "--use",
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        auth_path = self.store / "contexts" / "personal" / "auth.json"
        self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8")), {"kind": "new-login"})
        self.assertEqual(json.loads((self.codex_home / "auth.json").read_text()), {"kind": "new-login"})

    def test_login_forwards_access_token_flag(self) -> None:
        fake_codex = self.root / "fake-codex-token"
        args_file = self.root / "args.json"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys

                pathlib.Path({str(args_file)!r}).write_text(json.dumps(sys.argv))
                home = pathlib.Path(os.environ["CODEX_HOME"])
                stdin_value = sys.stdin.read()
                assert stdin_value == "token-value"
                (home / "auth.json").write_text(json.dumps({{"kind": "access-token"}}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "login",
            "automation",
            "--with-access-token",
        ]
        result = subprocess.run(cmd, input="token-value", text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(args_file.read_text(encoding="utf-8")), [str(fake_codex), "login", "--with-access-token"])

    def test_login_forwards_device_auth_flag(self) -> None:
        fake_codex = self.root / "fake-codex-device"
        args_file = self.root / "device-args.json"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys

                pathlib.Path({str(args_file)!r}).write_text(json.dumps(sys.argv))
                home = pathlib.Path(os.environ["CODEX_HOME"])
                (home / "auth.json").write_text(json.dumps({{"kind": "device-login"}}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "login",
            "ssh-login",
            "--device-auth",
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(args_file.read_text(encoding="utf-8")), [str(fake_codex), "login", "--device-auth"])

    def test_login_forwards_api_key_flag_and_stdin(self) -> None:
        fake_codex = self.root / "fake-codex-api-key"
        args_file = self.root / "api-key-args.json"
        stdin_file = self.root / "api-key-stdin.txt"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib
                import sys

                pathlib.Path({str(args_file)!r}).write_text(json.dumps(sys.argv))
                pathlib.Path({str(stdin_file)!r}).write_text(sys.stdin.read())
                home = pathlib.Path(os.environ["CODEX_HOME"])
                (home / "auth.json").write_text(json.dumps({{"kind": "api-key-login"}}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "login",
            "api-key-login",
            "--with-api-key",
        ]
        result = subprocess.run(cmd, input="sk-test\n", text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(args_file.read_text(encoding="utf-8")), [str(fake_codex), "login", "--with-api-key"])
        self.assertEqual(stdin_file.read_text(encoding="utf-8"), "sk-test\n")

    def test_login_with_api_key_stores_base_url(self) -> None:
        fake_codex = self.root / "fake-codex-api-key-base-url"
        fake_codex.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import pathlib

                home = pathlib.Path(os.environ["CODEX_HOME"])
                config = (home / "config.toml").read_text()
                assert 'model_provider = "openai"' in config
                assert 'openai_base_url = "https://api.example.test/v1"' in config
                assert pathlib.Path(os.environ["CODEX_HOME"]).name == "api-key-with-base"
                assert pathlib.Path(os.environ["CODEX_HOME"]).is_dir()
                (home / "auth.json").write_text(json.dumps({"kind": "api-key-login"}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "login",
            "api-key-with-base",
            "--with-api-key",
            "--base-url",
            "https://api.example.test/v1",
        ]
        result = subprocess.run(cmd, input="sk-test\n", text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.store / "contexts" / "api-key-with-base" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('openai_base_url = "https://api.example.test/v1"', config)

    def test_run_sets_isolated_codex_home_and_syncs_auth(self) -> None:
        self.assertEqual(self.run_switcher("capture", "custom-context").returncode, 0)
        helper = self.root / "helper.py"
        helper.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path

                home = Path(os.environ["CODEX_HOME"])
                assert home.name == "custom-context"
                (home / "auth.json").write_text(json.dumps({"kind": "refreshed"}) + "\\n")
                """
            ),
            encoding="utf-8",
        )
        result = self.run_switcher("run", "custom-context", "--", sys.executable, str(helper))
        self.assertEqual(result.returncode, 0, result.stderr)
        auth_path = self.store / "contexts" / "custom-context" / "auth.json"
        self.assertEqual(json.loads(auth_path.read_text(encoding="utf-8")), {"kind": "refreshed"})

    def test_list_fetches_fresh_rate_limits_for_signed_in_openai_context(self) -> None:
        self.run_switcher(
            "provider",
            "openai-work",
            "--provider-id",
            "openai",
            "--model",
            "gpt-5.5",
        )
        context_dir = self.store / "contexts" / "openai-work"
        (context_dir / "auth.json").write_text('{"kind":"auth"}\n', encoding="utf-8")
        fake_codex = self.root / "fake-codex-limits"
        fake_codex.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import sys

                for line in sys.stdin:
                    message = json.loads(line)
                    if message.get("method") == "initialize":
                        print(json.dumps({"id": message["id"], "result": {"codexHome": "/tmp/fake"}}), flush=True)
                    elif message.get("method") == "account/rateLimits/read":
                        print(json.dumps({
                            "id": message["id"],
                            "result": {
                                "rateLimits": {
                                    "limitId": "codex",
                                    "planType": "plus",
                                    "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1800000000},
                                    "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 1800100000}
                                },
                                "rateLimitsByLimitId": None
                            }
                        }), flush=True)
                        break
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(fake_codex),
            "list",
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("88%", result.stdout)
        self.assertIn("66%", result.stdout)
        cache = json.loads((context_dir / "rate_limits.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["snapshot"]["planType"], "plus")

    def test_list_reports_missing_codex_binary_as_unavailable_rate_limits(self) -> None:
        self.run_switcher(
            "provider",
            "openai-work",
            "--provider-id",
            "openai",
            "--model",
            "gpt-5.5",
        )
        context_dir = self.store / "contexts" / "openai-work"
        (context_dir / "auth.json").write_text('{"kind":"auth"}\n', encoding="utf-8")
        cmd = [
            sys.executable,
            str(SCRIPT),
            "--codex-home",
            str(self.codex_home),
            "--store",
            str(self.store),
            "--codex-bin",
            str(self.root / "missing-codex"),
            "list",
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("openai-work", result.stdout)
        self.assertIn("unavailable", result.stdout)

    def test_rate_limit_summary_reports_percent_remaining(self) -> None:
        summary = codexswitcher.summarize_rate_limits(
            {
                "planType": "plus",
                "primary": {"usedPercent": 12, "resetsAt": 1800000000},
                "secondary": {"usedPercent": 34, "resetsAt": 1800100000},
            },
            source="cached",
        )
        self.assertEqual(summary["five_hour"], "88%")
        self.assertEqual(summary["weekly"], "66%")
        self.assertIn("88% remaining", summary["five_hour_detail"])

    def test_limit_refresh_runs_contexts_concurrently(self) -> None:
        switcher = codexswitcher.Switcher(self.store, self.codex_home, "codex")
        started: list[str] = []
        original_summary = switcher.context_limit_summary

        for name in ("one", "two", "three"):
            context = self.store / "contexts" / name
            context.mkdir(parents=True)
            (context / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
            (context / "auth.json").write_text("{}\n", encoding="utf-8")

        def fake_summary(context_dir: Path, config: dict[str, object], refresh: bool = False) -> dict[str, str]:
            del config, refresh
            started.append(context_dir.name)
            time.sleep(0.25)
            return codexswitcher.empty_limit_summary(context_dir.name, context_dir.name)

        switcher.context_limit_summary = fake_summary  # type: ignore[method-assign]
        try:
            started_at = time.monotonic()
            queue = codexswitcher.start_limit_refresh(
                switcher,
                [
                    {"name": "one", "auth": "yes", "provider_auth": "codex-auth"},
                    {"name": "two", "auth": "yes", "provider_auth": "codex-auth"},
                    {"name": "three", "auth": "yes", "provider_auth": "codex-auth"},
                ],
            )
            results: dict[str | None, dict[str, str]] = {}
            while None not in results:
                name, summary = queue.get(timeout=2)
                results[name] = summary
            elapsed = time.monotonic() - started_at
        finally:
            switcher.context_limit_summary = original_summary  # type: ignore[method-assign]

        self.assertLess(elapsed, 0.6)
        self.assertCountEqual(started, ["one", "two", "three"])
        self.assertEqual(results["one"]["five_hour"], "one")
        self.assertEqual(results["two"]["five_hour"], "two")
        self.assertEqual(results["three"]["five_hour"], "three")

    def test_limit_refresh_reports_unexpected_worker_errors(self) -> None:
        switcher = codexswitcher.Switcher(self.store, self.codex_home, "codex")
        original_summary = switcher.context_limit_summary
        context = self.store / "contexts" / "gmail-bob"
        context.mkdir(parents=True)
        (context / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
        (context / "auth.json").write_text("{}\n", encoding="utf-8")

        def fake_summary(context_dir: Path, config: dict[str, object], refresh: bool = False) -> dict[str, str]:
            del context_dir, config, refresh
            raise RuntimeError("boom")

        switcher.context_limit_summary = fake_summary  # type: ignore[method-assign]
        try:
            queue = codexswitcher.start_limit_refresh(
                switcher,
                [{"name": "gmail-bob", "auth": "yes", "provider_auth": "codex-auth"}],
            )
            name, summary = queue.get(timeout=2)
            finish_name, finish_summary = queue.get(timeout=2)
        finally:
            switcher.context_limit_summary = original_summary  # type: ignore[method-assign]

        self.assertEqual(name, "gmail-bob")
        self.assertEqual(summary["five_hour"], "unavailable")
        self.assertEqual(summary["weekly"], "unavailable")
        self.assertIn("fetch failed: boom", summary["limits_cache"])
        self.assertIsNone(finish_name)
        self.assertEqual(finish_summary, {})

    def test_windows_tui_exit_sequence_moves_prompt_below_frame(self) -> None:
        self.assertEqual(
            codexswitcher.windows_tui_exit_sequence(17),
            "\x1b[?2026l\x1b[?25h\x1b[0m\x1b[17;1H\x1b[0K\n",
        )

    @unittest.skipUnless(os.name == "nt", "Windows launcher smoke test")
    def test_powershell_launcher_runs_python_version(self) -> None:
        result = subprocess.run(
            [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "codexswitcher.ps1"),
                "--codex-home",
                str(self.codex_home),
                "--store",
                str(self.store),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CODEX_HOME: {self.codex_home}", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows installer smoke test")
    def test_install_windows_creates_working_launchers(self) -> None:
        bin_dir = self.root / "bin"
        install = subprocess.run(
            [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "install-windows.ps1"),
                "-BinDirectory",
                str(bin_dir),
                "-Copy",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertTrue((bin_dir / "cdxsw.ps1").exists())
        self.assertTrue((bin_dir / "cdxsw.cmd").exists())
        self.assertTrue((bin_dir / "cdxsw").exists())

        status = subprocess.run(
            [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(bin_dir / "cdxsw.ps1"),
                "--codex-home",
                str(self.codex_home),
                "--store",
                str(self.store),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f"CODEX_HOME: {self.codex_home}", status.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows masked prompt test")
    def test_windows_secret_prompt_masks_each_character(self) -> None:
        chars = iter(["s", "k", "\b", "x", "\r"])

        with (
            mock.patch.object(codexswitcher.sys, "stdout", StringIO()) as stdout,
            mock.patch("msvcrt.getwch", side_effect=lambda: next(chars)),
        ):
            value = codexswitcher.read_windows_secret("Provider API key: ")

        self.assertEqual(value, "sx")
        self.assertEqual(stdout.getvalue(), "Provider API key: **\b \b*\n")

    def test_interactive_new_context_menus_hide_advanced_secret_modes(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('("access-token", "Codex access token")', source)
        self.assertNotIn('("provider-env", "Custom provider using an environment variable")', source)
        self.assertNotIn('("access-token", "Codex access token from hidden prompt")', source)


if __name__ == "__main__":
    unittest.main()
