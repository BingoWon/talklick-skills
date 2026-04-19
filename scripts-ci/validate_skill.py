#!/usr/bin/env python3
"""
Validate a SKILL.md file's YAML frontmatter against Anthropic's
Agent Skills standard (https://agentskills.io).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def fail(msg: str) -> None:
    sys.stderr.write(f"validate_skill: {msg}\n")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: validate_skill.py <path/to/SKILL.md>")
    path = Path(sys.argv[1])
    if path.name != "SKILL.md":
        fail(f"file must be named exactly SKILL.md (got {path.name})")

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail("must start with YAML frontmatter delimiter '---'")
    end = text.find("\n---\n", 4)
    if end == -1:
        fail("frontmatter must be closed with '---' on its own line")

    front = text[4:end]
    fields = _parse_simple_yaml(front)

    name = fields.get("name")
    if not name or not NAME_RE.match(name):
        fail(f"name must be kebab-case (got {name!r})")
    if any(reserved in name.lower() for reserved in ("claude", "anthropic")):
        fail("name may not contain 'claude' or 'anthropic' (reserved)")

    desc = fields.get("description")
    if not desc:
        fail("description is required")
    if len(desc) > 1024:
        fail(f"description must be ≤1024 chars (got {len(desc)})")
    if "<" in desc or ">" in desc:
        fail("description may not contain '<' or '>' (security)")

    print(f"ok: {path} — name={name}, description={len(desc)} chars")


def _parse_simple_yaml(text: str) -> dict[str, str]:
    """Minimal YAML subset: top-level `key: value` and `key: >- …` blocks."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()

        if rest in (">-", ">", "|", "|-"):
            folded = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                folded.append(lines[i].strip())
                i += 1
            value = " ".join(s for s in folded if s)
        else:
            value = rest
            i += 1

        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key] = value
    return out


if __name__ == "__main__":
    main()
