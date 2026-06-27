"""Parser for the .nexus job DSL.

A .nexus file is the human-friendly form of the JSON payload accepted by
``POST /api/jobs``. The parser produces a dict that matches the
``JobSubmit`` schema; callers can validate / mutate / submit it as usual.

Grammar (informal):

    # name: <string>          -- job-level metadata
    # pool: <pool name>       -- resolved to target_pool_id by caller
    # node: <node uuid>       -- resolved to target_node_id by caller

    @set("k": "v", ...)       -- parse-time string variables

    step_name("k": value, ...) [-> $a, $b] [on_fail="continue"]

Inside the ``(...)`` body, ``"k": value`` pairs use JSON literal syntax
(strings, numbers, lists, objects, true/false/null). ``${var}`` is
resolved at parse time when the variable was declared with ``@set``;
otherwise the literal ``${var}`` is preserved so the runner can resolve
it from upstream step captures (the ``-> $captures`` syntax).

Reserved kwargs (extracted out of ``params`` and onto the step itself):

    on_fail        -- "stop" (default) or "continue"
    target_os      -- "macos" / "linux" / "windows"
    target_node_id -- pin to a specific node
    target_pool_id -- restrict to a specific pool

Example:

    # name: smoke
    @set("repo": "https://example.com/x.git")

    git_clone("url": "${repo}", "target_os": "linux") -> $clone_dir
    run_python("code": "print('hi')", "target_node_id": "abc-123") on_fail="continue"
    jump("target_step": 0, "on": "fail")

This parser is intentionally minimal — it's the smallest thing that round-
trips to JobSubmit. Folder grouping and @params permutation expansion
(features inherited from HVEAW's UI builder) are deliberately out of scope
until we know we need them.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ── Public surface ───────────────────────────────────────────────────────


def parse_nexus_string(text: str) -> dict[str, Any]:
    """Parse a .nexus DSL string into a JobSubmit-shaped dict.

    Returns:
        ``{"name": str | None, "_pool_name": str | None,
           "_node_id": str | None, "steps": [StepConfig-shaped dicts]}``

    The ``_pool_name`` / ``_node_id`` keys are private — the caller is
    expected to resolve them to UUIDs (e.g. via ``ops.list_pools``) before
    submitting the JobSubmit payload. Raises ``NexusParseError`` on
    malformed input.
    """
    metadata: dict[str, str] = {}
    set_vars: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        # Metadata comments: "# name: foo"
        if line.startswith("#"):
            m = _META_LINE.match(line)
            if m:
                metadata[m.group("key").lower()] = m.group("value").strip()
            continue

        # @set("k": "v", ...)
        if line.startswith("@set"):
            try:
                set_vars.update(_parse_set_literal(line, set_vars))
            except (ValueError, json.JSONDecodeError) as exc:
                raise NexusParseError(line_no, f"invalid @set: {exc}") from exc
            continue

        # step_name(...) -> $a, $b on_fail="continue"
        try:
            steps.append(_parse_step_line(line, set_vars))
        except (ValueError, json.JSONDecodeError) as exc:
            raise NexusParseError(line_no, f"invalid step: {exc}") from exc

    return {
        "name": metadata.get("name"),
        "_pool_name": metadata.get("pool"),
        "_node_id": metadata.get("node"),
        "steps": steps,
    }


class NexusParseError(ValueError):
    """Raised when the .nexus source can't be parsed."""

    def __init__(self, line_no: int, detail: str) -> None:
        super().__init__(f"line {line_no}: {detail}")
        self.line_no = line_no
        self.detail = detail


# ── Internals ────────────────────────────────────────────────────────────


_META_LINE = re.compile(r"^#\s*(?P<key>[A-Za-z_][\w-]*)\s*:\s*(?P<value>.+?)\s*$")
_VAR_REF = re.compile(r"\$\{([A-Za-z_][\w]*)\}")
_STEP_HEAD = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\s*\(")

# Reserved keys that get lifted out of params onto the step record itself.
_STEP_KEYWORDS = {"on_fail", "target_os", "target_node_id", "target_pool_id"}


def _balanced_paren_body(line: str, open_idx: int) -> tuple[str, int]:
    """Return (body_inside_parens, index_after_close)."""
    depth = 0
    in_str: str | None = None
    escape = False
    for i in range(open_idx, len(line)):
        ch = line[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[open_idx + 1 : i], i + 1
    raise ValueError("unterminated '(' in step call")


def _substitute_vars(value: Any, set_vars: dict[str, str]) -> Any:
    """Replace ``${var}`` with @set values in strings; leave unknown vars intact.

    Lists / dicts are walked recursively. Non-strings are returned unchanged.
    """
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            return set_vars[name] if name in set_vars else match.group(0)

        return _VAR_REF.sub(repl, value)
    if isinstance(value, list):
        return [_substitute_vars(v, set_vars) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_vars(v, set_vars) for k, v in value.items()}
    return value


def _parse_kv_body(body: str) -> dict[str, Any]:
    """Parse a `"k": value, "k2": value2` body into a dict via JSON.

    Wrapping in ``{ ... }`` and delegating to ``json.loads`` keeps quoting
    rules consistent with the rest of the system (the JSON payload format).
    """
    body = body.strip()
    if not body:
        return {}
    return json.loads("{" + body + "}")


def _parse_set_literal(line: str, set_vars: dict[str, str]) -> dict[str, str]:
    open_idx = line.index("(")
    body, _ = _balanced_paren_body(line, open_idx)
    parsed = _parse_kv_body(body)
    out: dict[str, str] = {}
    for k, v in parsed.items():
        # @set values must be string-typed for predictable substitution.
        if not isinstance(v, str):
            raise ValueError(f"@set value for '{k}' must be a string")
        out[k] = _substitute_vars(v, {**set_vars, **out})
    return out


def _parse_step_line(line: str, set_vars: dict[str, str]) -> dict[str, Any]:
    head = _STEP_HEAD.match(line)
    if not head:
        raise ValueError(f"expected `step_name(...)`: {line!r}")
    step_name = head.group("name")
    open_idx = head.end() - 1
    body, after = _balanced_paren_body(line, open_idx)

    raw_params = _parse_kv_body(body)
    raw_params = _substitute_vars(raw_params, set_vars)

    captures, trailing = _split_trailing(line[after:].strip())
    keyword_overrides = _parse_trailing_keywords(trailing)

    # Lift reserved keywords out of params so they ride on the StepConfig.
    step_record: dict[str, Any] = {
        "step": step_name,
        "params": {},
        "on_fail": "stop",
    }
    for key, val in raw_params.items():
        if key in _STEP_KEYWORDS:
            step_record[key] = val
        else:
            step_record["params"][key] = val

    # Trailing keywords override anything inside params (explicit beats implicit).
    for key, val in keyword_overrides.items():
        step_record[key] = val

    if captures:
        # Captured outputs feed back into the runner's runtime context as
        # the named keys; the parser just records them as a hint.
        step_record["_captures"] = captures

    return step_record


def _split_trailing(trailing: str) -> tuple[list[str], str]:
    """Split `-> $a, $b on_fail="continue"` into (captures, leftover).

    Captures are the ``$`` names after the arrow; the remainder is parsed
    as `key="value"` keyword overrides.
    """
    captures: list[str] = []
    if trailing.startswith("->"):
        rest = trailing[2:].lstrip()
        # Take consecutive `$name` tokens separated by commas.
        m = re.match(r"((?:\$[A-Za-z_][\w]*\s*,?\s*)+)", rest)
        if m:
            for tok in re.findall(r"\$([A-Za-z_][\w]*)", m.group(1)):
                captures.append(tok)
            trailing = rest[m.end():].lstrip()
        else:
            trailing = rest
    return captures, trailing


def _parse_trailing_keywords(trailing: str) -> dict[str, Any]:
    """Parse `key="value" key2="value"` into a dict.

    Currently only quoted-string values are supported — these are step-
    level overrides like ``on_fail="continue"`` or ``target_os="linux"``.
    """
    out: dict[str, Any] = {}
    if not trailing:
        return out
    for m in re.finditer(r"(?P<k>[A-Za-z_][\w]*)\s*=\s*\"(?P<v>[^\"]*)\"", trailing):
        out[m.group("k")] = m.group("v")
    return out
