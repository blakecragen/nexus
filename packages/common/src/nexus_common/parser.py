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

Structure of this module
------------------------
``parse_nexus_string`` is the only public entry point; everything below the
"Internals" banner is a line-level helper it drives. Parsing is strictly
line-oriented — a step call must fit on one line, there is no statement
continuation, and blank lines are skipped. Errors are always raised as
``NexusParseError`` carrying the 1-based source line number.

AI Note: This module has no dependency on the step registry and never checks
that a step name exists or that its params are valid. That is deliberate — the
parser only builds a payload; ``POST /api/jobs`` performs the real validation
against ``STEP_REGISTRY``. Keep it that way so the DSL can be parsed anywhere
(CLI, tests) without importing the step implementations.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ── Public surface ───────────────────────────────────────────────────────


def parse_nexus_string(text: str) -> dict[str, Any]:
    """Parse a .nexus DSL string into a JobSubmit-shaped dict.

    Walks the source one line at a time, classifying each as metadata comment,
    ``@set`` declaration, or step call. ``@set`` variables accumulate as parsing
    proceeds, so a variable is only visible to steps *below* its declaration.

    Args:
        text: Full ``.nexus`` source. Line endings and blank lines are handled;
            each non-blank line must be a complete statement.

    Returns:
        ``{"name": str | None, "_pool_name": str | None,
           "_node_id": str | None, "steps": [StepConfig-shaped dicts]}``

    The ``_pool_name`` / ``_node_id`` keys are private — the caller is
    expected to resolve them to UUIDs (e.g. via ``ops.list_pools``) before
    submitting the JobSubmit payload. Raises ``NexusParseError`` on
    malformed input.

    Raises:
        NexusParseError: On any malformed ``@set`` or step line, tagged with the
            1-based line number.

    AI Note: Unrecognized ``# key: value`` comments are collected into the
    metadata dict and then silently dropped — only name/pool/node are read out.
    A typo like ``# poool: x`` therefore parses cleanly and produces an untargeted
    job rather than an error.
    """
    metadata: dict[str, str] = {}
    set_vars: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        # Metadata comments: "# name: foo"
        # AI Note: keys are lowercased so "# Name:" and "# name:" are equivalent,
        # but the *value* keeps its original case (job names are user-visible).
        if line.startswith("#"):
            m = _META_LINE.match(line)
            if m:
                metadata[m.group("key").lower()] = m.group("value").strip()
            continue

        # @set("k": "v", ...)
        # AI Note: `set_vars` is passed into the parse so a later @set can
        # reference an earlier one; declaration order is significant and forward
        # references are impossible by construction.
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
    """Raised when the .nexus source can't be parsed.

    Subclasses ``ValueError`` so callers that only catch ValueError still handle
    it, while the structured attributes let editors point at the offending line.

    Args:
        line_no: 1-based source line where parsing failed.
        detail: What went wrong, already prefixed with the failing construct
            ("invalid @set: ..." / "invalid step: ...").

    Attributes:
        line_no: The line number, preserved separately from the message.
        detail: The reason, without the "line N: " prefix.
    """

    def __init__(self, line_no: int, detail: str) -> None:
        super().__init__(f"line {line_no}: {detail}")
        self.line_no = line_no
        self.detail = detail


# ── Internals ────────────────────────────────────────────────────────────


# Matches "# key: value" metadata comments. The key charset excludes ':' so the
# first colon always terminates it; the value is non-greedy but anchored to
# end-of-line, so a value containing colons (e.g. "http://host:8080/x") survives.
_META_LINE = re.compile(r"^#\s*(?P<key>[A-Za-z_][\w-]*)\s*:\s*(?P<value>.+?)\s*$")

# Matches a ${var} reference inside a string value.
_VAR_REF = re.compile(r"\$\{([A-Za-z_][\w]*)\}")

# Matches the "step_name(" prefix of a step call; the body is then scanned by
# _balanced_paren_body rather than by regex, since parens can nest.
_STEP_HEAD = re.compile(r"^(?P<name>[A-Za-z_][\w]*)\s*\(")

# Reserved keys that get lifted out of params onto the step record itself.
#
# AI Note: This set must stay in sync with the non-``params`` fields of
# ``schemas.StepConfig``. If it drifts, a user writing e.g. target_os inside the
# parameter body gets it passed through as a step parameter, and submission fails
# with "unknown parameter" because the step's PARAMS_SCHEMA rejects it.
_STEP_KEYWORDS = {"on_fail", "target_os", "target_node_id", "target_pool_id"}


def _balanced_paren_body(line: str, open_idx: int) -> tuple[str, int]:
    """Return (body_inside_parens, index_after_close).

    Hand-rolled scanner rather than a regex because the body may contain nested
    parens (inside JSON objects/lists) and parens inside quoted strings, neither
    of which a regex can match reliably.

    Args:
        line: The full source line.
        open_idx: Index of the opening ``(``.

    Returns:
        Tuple of the text between the parens (exclusive) and the index just past
        the matching ``)``.

    Raises:
        ValueError: If the parenthesis is never closed on this line.

    AI Note: The scanner tracks quote state (both ``"`` and ``'``) and backslash
    escapes, so ``run("cmd": "echo (hi)")`` does not terminate early. Escapes are
    only honoured *inside* a string — a stray backslash outside one is ignored,
    matching JSON's own rules since the body is later handed to ``json.loads``.
    """
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

    Args:
        value: Any already-JSON-decoded value (str, list, dict, or scalar).
        set_vars: Variables declared by ``@set`` so far.

    Returns:
        The value with known ``${var}`` references expanded. Containers are
        rebuilt rather than mutated in place.

    AI Note: Leaving unknown vars intact is the whole point, not laziness. A
    ``${clone_dir}`` that refers to an upstream step's capture must survive
    parsing untouched so the runner can resolve it from the job context at
    dispatch time. Raising on unknown names here would make step chaining
    impossible. Substitution is single-pass: an @set value that itself expands to
    a literal "${x}" is not re-scanned.
    """
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            """Expand one ``${name}`` match, or return it verbatim if unknown."""
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

    Args:
        body: Text between the call's parentheses, without the parens.

    Returns:
        Decoded mapping; ``{}`` for an empty or whitespace-only body.

    Raises:
        json.JSONDecodeError: On malformed content. The caller wraps this in a
            ``NexusParseError`` with the line number attached.

    AI Note: Delegating to ``json.loads`` is what forces keys to be
    double-quoted and rejects trailing commas and single-quoted strings. That
    strictness is intentional — the DSL body is literally JSON-object innards, so
    anything valid here is valid in the JSON API payload and vice versa.
    """
    body = body.strip()
    if not body:
        return {}
    return json.loads("{" + body + "}")


def _parse_set_literal(line: str, set_vars: dict[str, str]) -> dict[str, str]:
    """Parse one ``@set("k": "v", ...)`` line into new variable bindings.

    Args:
        line: The full ``@set`` source line.
        set_vars: Variables already declared, so a new value may reference them.

    Returns:
        Only the bindings declared on *this* line (the caller merges them into the
        running variable map).

    Raises:
        ValueError: If any value is not a JSON string, or the parens are unbalanced.
        json.JSONDecodeError: If the body is not valid JSON-object innards.

    AI Note: Values are expanded against ``{**set_vars, **out}`` — the partial
    result of this same line — so bindings on one ``@set`` may reference earlier
    bindings on that line. Since ``json.loads`` preserves source order, this is
    left-to-right and deterministic.
    """
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
    """Parse one ``step_name(...) [-> $a, $b] [kw="v"]`` line into a step record.

    Args:
        line: The full step source line.
        set_vars: Variables declared by ``@set`` above this line.

    Returns:
        A ``StepConfig``-shaped dict: ``step``, ``params``, ``on_fail``, plus any
        lifted ``target_*`` keys and an optional private ``_captures`` list.

    Raises:
        ValueError: If the line does not start with ``step_name(`` or the parens
            are unbalanced.
        json.JSONDecodeError: If the parameter body is not valid JSON innards.

    AI Note: Precedence is fixed and tested: OS-independent params < reserved
    keywords written inside the parameter body < trailing ``kw="value"`` overrides.
    Explicit (trailing) beats implicit (in-body), which is why the override loop
    runs after the lift loop.
    """
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

    Args:
        trailing: Everything on the line after the step call's closing paren,
            already stripped.

    Returns:
        Tuple of the capture names (``$`` stripped, empty if no arrow) and the
        unconsumed remainder for keyword parsing.

    AI Note: The capture regex only consumes a *run* of consecutive ``$name``
    tokens, so scanning stops at the first non-capture token and hands the rest
    back as keyword text. That is what lets ``-> $a, $b on_fail="continue"`` parse
    without a separator. If the text after ``->`` does not start with a ``$name``,
    nothing is captured and the whole remainder falls through to keyword parsing —
    a malformed arrow is silently ignored rather than reported.
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

    Args:
        trailing: Leftover text after captures were consumed. May be empty.

    Returns:
        Mapping of override key to string value; ``{}`` when there is nothing to parse.

    AI Note: This uses ``finditer``, not a full-line match, so anything that does
    not look like ``key="value"`` is skipped without error. Unquoted values
    (``on_fail=continue``) and non-string values are therefore dropped silently
    rather than rejected — a known sharp edge of the minimal DSL.
    """
    out: dict[str, Any] = {}
    if not trailing:
        return out
    for m in re.finditer(r"(?P<k>[A-Za-z_][\w]*)\s*=\s*\"(?P<v>[^\"]*)\"", trailing):
        out[m.group("k")] = m.group("v")
    return out
