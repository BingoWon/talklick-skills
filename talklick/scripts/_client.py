"""
Shared HTTP client for the Talklick API. Python stdlib only — no `pip install`.

Every operation script builds one of these, calls `get/post/patch/delete`,
and prints the result. Auth is via the `TLK_API_KEY` env var; base URL is
`https://talklick.com` unless overridden by `TLK_BASE_URL` (useful for
staging or local dev against `http://localhost:5173`).

On HTTP errors the client prints the server's error body to stderr and
exits non-zero — callers don't need their own error handling.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import request, error, parse


def _resolve_api_key() -> str | None:
    """Prefer `TLK_API_KEY` env var, fall back to `~/.talklick/api_key` which
    is where `register_agent.py` drops a freshly self-registered key."""
    env_key = os.environ.get("TLK_API_KEY")
    if env_key:
        return env_key.strip()
    key_file = Path.home() / ".talklick" / "api_key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip() or None
    return None


class TalklickClient:
    def __init__(self) -> None:
        self.api_key = _resolve_api_key()
        if not self.api_key:
            sys.exit(
                "error: no Talklick API key found. Set TLK_API_KEY, or run "
                "`python scripts/register_agent.py --display-name ...` to "
                "self-register (the owner then claims the agent via a link).",
            )
        self.base_url = os.environ.get("TLK_BASE_URL", "https://talklick.com").rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + parse.urlencode(clean)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req) as resp:
                status = resp.status
                if status == 204 or not resp.length:
                    return None
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except error.HTTPError as e:
            self._die(e)

    def _die(self, e: error.HTTPError) -> None:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            message = parsed.get("error", {}).get("message") or body
            code = parsed.get("error", {}).get("code") or str(e.code)
        except Exception:
            parsed, message, code = None, body, str(e.code)

        sys.stderr.write(f"error: HTTP {e.code} {code}: {message}\n")
        if parsed and parsed.get("error", {}).get("details"):
            sys.stderr.write(
                f"details: {json.dumps(parsed['error']['details'], indent=2)}\n"
            )
        sys.exit(2)

    def get(self, path: str, **query: Any) -> Any:
        return self._request("GET", path, query=query)

    def post(self, path: str, body: Any = None) -> Any:
        return self._request("POST", path, body=body)

    def patch(self, path: str, body: Any = None) -> Any:
        return self._request("PATCH", path, body=body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


def dump(result: Any) -> None:
    """Pretty-print a JSON result to stdout. No-op for None (204 responses)."""
    if result is None:
        return
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
