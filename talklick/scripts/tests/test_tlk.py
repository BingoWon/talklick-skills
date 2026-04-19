"""
Unit tests for `tlk.py` — the generic op runner.

We verify the pure logic (flag parsing, path rendering, method / placement
dispatch) and, via `urlopen` mocks, the wire-level HTTP shape. The real
worker is not involved — that layer is covered by `worker/agent-api.test.ts`.

Run via: `pnpm test:skills`
         (or: `python3 -m unittest discover -s skills/talklick/scripts/tests
              -t skills/talklick/scripts`)
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tlk  # noqa: E402  — sys.path manipulation must precede import


# ─── helpers ────────────────────────────────────────────────────────────

class _UrlopenCapture:
    """Fake `urllib.request.urlopen` that records the outgoing request and
    returns a JSON response. Keeps the last request so tests can assert on
    method / URL / body without caring about the HTTP stack in `_client.py`."""

    def __init__(self, response: bytes = b'{"ok":true}', status: int = 200) -> None:
        self._response = response
        self._status = status
        self.last_req = None

    def __call__(self, req):
        self.last_req = req
        mock = MagicMock()
        ctx = mock.__enter__.return_value
        ctx.read.return_value = self._response
        ctx.status = self._status
        ctx.length = len(self._response)
        return mock


def _with_env(**kwargs):
    """setUp helper — set env vars for the test, restore on tearDown."""
    previous = {k: os.environ.get(k) for k in kwargs}

    def restore():
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    os.environ.update({k: v for k, v in kwargs.items() if v is not None})
    return restore


# ─── flag parsing ──────────────────────────────────────────────────────

class ParseFlagPairs(unittest.TestCase):
    def test_dashes_become_underscores(self) -> None:
        self.assertEqual(
            tlk.parse_flag_pairs(["--conversation-id", "conv_123"]),
            {"conversation_id": "conv_123"},
        )

    def test_json_arrays_are_decoded(self) -> None:
        self.assertEqual(
            tlk.parse_flag_pairs(["--tags", '["a","b"]']),
            {"tags": ["a", "b"]},
        )

    def test_json_numbers_are_decoded(self) -> None:
        self.assertEqual(tlk.parse_flag_pairs(["--limit", "50"]), {"limit": 50})

    def test_json_booleans_are_decoded(self) -> None:
        self.assertEqual(tlk.parse_flag_pairs(["--pinned", "true"]), {"pinned": True})
        self.assertEqual(tlk.parse_flag_pairs(["--pinned", "false"]), {"pinned": False})

    def test_plain_strings_survive_untouched(self) -> None:
        # Unquoted free text that isn't valid JSON stays a string — the common
        # case for message content, bios, tags, etc.
        self.assertEqual(
            tlk.parse_flag_pairs(["--text", "hello world"]),
            {"text": "hello world"},
        )

    def test_equals_form_parses(self) -> None:
        self.assertEqual(tlk.parse_flag_pairs(["--text=hi"]), {"text": "hi"})

    def test_multiple_flags_accumulate(self) -> None:
        self.assertEqual(
            tlk.parse_flag_pairs(
                ["--conversation-id", "conv_x", "--text", "hi", "--client-id", "uuid"],
            ),
            {"conversation_id": "conv_x", "text": "hi", "client_id": "uuid"},
        )

    def test_missing_value_exits(self) -> None:
        with self.assertRaises(SystemExit):
            tlk.parse_flag_pairs(["--text"])

    def test_non_flag_token_exits(self) -> None:
        with self.assertRaises(SystemExit):
            tlk.parse_flag_pairs(["positional_without_flag"])


# ─── manifest load ─────────────────────────────────────────────────────

class LoadOps(unittest.TestCase):
    def test_reads_generated_ops_json(self) -> None:
        ops = tlk.load_ops()
        self.assertIn("get_self", ops)
        self.assertIn("send_message", ops)
        self.assertEqual(ops["get_self"]["method"], "GET")
        self.assertEqual(ops["get_self"]["path"], "/v1/self")
        self.assertEqual(ops["send_message"]["bucket"], "write")
        self.assertEqual(
            ops["dissolve_group"]["bucket"],
            "destructive",
            "Destructive ops must be so-tagged — the MCP dispatcher relies on this",
        )


# ─── end-to-end dispatch (mocked HTTP) ─────────────────────────────────

class Dispatch(unittest.TestCase):
    """Drives `tlk.main()` with mocked `urlopen` to assert URL / method / body."""

    def setUp(self) -> None:
        # Pin env so TalklickClient doesn't read a real key off the dev box.
        self._restore_env = _with_env(
            TLK_API_KEY="tlk_test_fake_0123456789",
            TLK_BASE_URL="http://test.local",
        )
        self.capture = _UrlopenCapture()

    def tearDown(self) -> None:
        self._restore_env()

    def _run(self, argv: list[str]) -> str:
        stdout = io.StringIO()
        with (
            patch("urllib.request.urlopen", self.capture),
            patch("sys.stdout", stdout),
            patch.object(sys, "argv", ["tlk.py", *argv]),
        ):
            tlk.main()
        return stdout.getvalue()

    def test_get_no_args_hits_bare_path(self) -> None:
        self._run(["get_self"])
        req = self.capture.last_req
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.full_url, "http://test.local/v1/self")
        self.assertIsNone(req.data)
        self.assertEqual(
            req.headers.get("Authorization"),
            "Bearer tlk_test_fake_0123456789",
            "Bearer token must be attached verbatim",
        )

    def test_path_param_rendered_into_url(self) -> None:
        self._run(["get_conversation", "--conversation-id", "conv_abc"])
        self.assertEqual(
            self.capture.last_req.full_url,
            "http://test.local/v1/conversations/conv_abc",
        )
        self.assertEqual(self.capture.last_req.method, "GET")

    def test_post_body_placement_serialises_args_as_json(self) -> None:
        self._run(
            [
                "create_conversation",
                "--type",
                "direct",
                "--participant-actor-id",
                "usr_abc",
            ],
        )
        req = self.capture.last_req
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "http://test.local/v1/conversations")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body, {"type": "direct", "participant_actor_id": "usr_abc"})

    def test_query_placement_serialises_args_into_url(self) -> None:
        self._run(["list_conversations", "--limit", "25"])
        url = self.capture.last_req.full_url
        self.assertIn("limit=25", url)
        self.assertTrue(url.startswith("http://test.local/v1/conversations?"))

    def test_delete_sends_no_body(self) -> None:
        self._run(["remove_friend", "--actor-id", "usr_xyz"])
        req = self.capture.last_req
        self.assertEqual(req.method, "DELETE")
        self.assertEqual(req.full_url, "http://test.local/v1/friends/usr_xyz")
        self.assertIsNone(req.data)

    def test_patch_body_placement(self) -> None:
        self._run(["update_self", "--bio", "hello"])
        req = self.capture.last_req
        self.assertEqual(req.method, "PATCH")
        self.assertEqual(req.full_url, "http://test.local/v1/self")
        self.assertEqual(json.loads(req.data.decode()), {"bio": "hello"})

    def test_missing_path_param_exits(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(["get_conversation"])  # needs --conversation-id

    def test_unknown_op_exits(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(["definitely_not_a_real_op"])

    def test_http_error_exits_non_zero(self) -> None:
        # A 400 with a JSON error body should exit 2 and write to stderr —
        # never silently print success. Replace the urlopen fake with one
        # that raises HTTPError so we exercise `_client.TalklickClient._die`.
        from urllib import error

        def raiser(req):  # noqa: ARG001
            raise error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":{"code":"INVALID_REQUEST","message":"bad"}}'),
            )

        stderr = io.StringIO()
        with (
            patch("urllib.request.urlopen", raiser),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", stderr),
            patch.object(sys, "argv", ["tlk.py", "get_self"]),
        ):
            with self.assertRaises(SystemExit) as ctx:
                tlk.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("INVALID_REQUEST", stderr.getvalue())


# ─── discovery (--list / --help) ───────────────────────────────────────

class Discover(unittest.TestCase):
    def test_list_prints_three_bucket_sections(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch.object(sys, "argv", ["tlk.py", "--list"]):
            tlk.main()
        out = stdout.getvalue()
        for section in ("── read ──", "── write ──", "── destructive ──"):
            self.assertIn(section, out)
        self.assertIn("get_self", out)
        self.assertIn("dissolve_group", out)

    def test_op_help_prints_specific_op_details(self) -> None:
        stdout = io.StringIO()
        with (
            patch("sys.stdout", stdout),
            patch.object(sys, "argv", ["tlk.py", "--help", "send_message"]),
        ):
            tlk.main()
        out = stdout.getvalue()
        self.assertIn("send_message", out)
        self.assertIn("POST", out)
        self.assertIn("write", out)
        self.assertIn("conversation_id", out)  # path param surfaced

    def test_op_help_unknown_op_exits(self) -> None:
        with (
            patch("sys.stdout", io.StringIO()),
            patch.object(sys, "argv", ["tlk.py", "--help", "no_such_op"]),
        ):
            with self.assertRaises(SystemExit):
                tlk.main()


if __name__ == "__main__":
    unittest.main()
