"""
Unit tests for `register_agent.py` — the skill-bootstrap entry point.

First-run failures here are the ones hardest to diagnose from the
outside (fresh env, no API key yet), so we cover the three things that
can go silently wrong: wrong file perms on the cached key, malformed
stdout that breaks downstream parsing, and unhandled HTTP errors.

Run via: `pnpm test:skills`
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import register_agent  # noqa: E402


# ─── key-file writer ────────────────────────────────────────────────────

class WriteKey(unittest.TestCase):
    def test_creates_parent_dir_on_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "subdir" / "api_key"
            register_agent._write_key(str(path), "tlk_test_xxx")
            self.assertTrue(path.exists())

    def test_writes_key_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_key"
            register_agent._write_key(str(path), "tlk_test_content")
            # A trailing newline is added so `cat` output is sane.
            self.assertEqual(path.read_text().strip(), "tlk_test_content")

    @unittest.skipIf(os.name == "nt", "POSIX-only permission model")
    def test_permissions_are_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_key"
            register_agent._write_key(str(path), "tlk_test_xxx")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(
                mode,
                0o600,
                "Key file must be 0600 — shared hosts and team Macs can read "
                "default-perm files, and this is our single long-lived secret",
            )


# ─── main flow ─────────────────────────────────────────────────────────

class MainFlow(unittest.TestCase):
    def _fake_response(self, payload: dict) -> MagicMock:
        mock = MagicMock()
        mock.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
        return mock

    def test_happy_path_writes_key_and_prints_claim_url(self) -> None:
        payload = {
            "actor_id": "agt_test",
            "api_key": "tlk_test_fake_key",
            "api_key_prefix": "tlk_test_fake_ke",
            "claim_url": "http://test.local/claim/token_xxx",
            "claim_token": "token_xxx",
            "claim_expires_at": "2026-04-19T00:00:00Z",
        }

        with tempfile.TemporaryDirectory() as tmp:
            key_path = str(Path(tmp) / "api_key")
            argv = [
                "register_agent.py",
                "--display-name",
                "Test Bot",
                "--base-url",
                "http://test.local",
                "--key-path",
                key_path,
            ]
            stdout = io.StringIO()
            with (
                patch("urllib.request.urlopen", return_value=self._fake_response(payload)),
                patch("sys.stdout", stdout),
                patch.object(sys, "argv", argv),
            ):
                register_agent.main()

            # Key persisted with right content.
            self.assertEqual(Path(key_path).read_text().strip(), "tlk_test_fake_key")

            out = stdout.getvalue()
            # The JSON envelope is machine-parseable.
            json_part = out.split("\n\n", 1)[0]
            parsed = json.loads(json_part)
            self.assertEqual(parsed["actor_id"], "agt_test")
            self.assertEqual(parsed["claim_url"], "http://test.local/claim/token_xxx")
            # And the human-readable tail carries the URL as a grep-ready line
            # so a skill runner can surface it to the owner verbatim.
            self.assertIn("Owner: open this link", out)
            self.assertIn("http://test.local/claim/token_xxx", out.splitlines()[-1])

    def test_optional_fields_are_sent_when_provided(self) -> None:
        payload = {
            "actor_id": "agt_x",
            "api_key": "tlk_test_x",
            "api_key_prefix": "tlk_test_x",
            "claim_url": "http://t.local/claim/t",
            "claim_token": "t",
            "claim_expires_at": "2026-04-19T00:00:00Z",
        }
        captured: dict[str, object] = {}

        def capturing_urlopen(req):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return self._fake_response(payload)

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "register_agent.py",
                "--display-name",
                "Fancy Bot",
                "--bio",
                "a short bio",
                "--tags",
                "one, two ,three",  # whitespace around commas must be trimmed
                "--capabilities",
                "long description",
                "--base-url",
                "http://t.local",
                "--key-path",
                str(Path(tmp) / "api_key"),
            ]
            with (
                patch("urllib.request.urlopen", side_effect=capturing_urlopen),
                patch("sys.stdout", io.StringIO()),
                patch.object(sys, "argv", argv),
            ):
                register_agent.main()

        body = captured["body"]
        self.assertEqual(body["display_name"], "Fancy Bot")
        self.assertEqual(body["bio"], "a short bio")
        self.assertEqual(
            body["tags"],
            ["one", "two", "three"],
            "Tags should be split + trimmed — 'one, two ,three' → three clean strings",
        )
        self.assertEqual(body["capabilities_description"], "long description")

    def test_http_error_exits_non_zero_with_stderr(self) -> None:
        from urllib import error

        def raiser(req):  # noqa: ARG001
            raise error.HTTPError(
                "http://t.local/v1/agents/register",
                400,
                "Bad Request",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":{"code":"INVALID_REQUEST","message":"missing display_name"}}'),
            )

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "register_agent.py",
                "--display-name",
                "X",
                "--base-url",
                "http://t.local",
                "--key-path",
                str(Path(tmp) / "api_key"),
            ]
            stderr = io.StringIO()
            with (
                patch("urllib.request.urlopen", side_effect=raiser),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", stderr),
                patch.object(sys, "argv", argv),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    register_agent.main()

            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("HTTP 400", stderr.getvalue())
            self.assertIn("INVALID_REQUEST", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
