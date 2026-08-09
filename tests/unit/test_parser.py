"""Unit tests for the .nexus DSL parser (nexus_common.parser).

Covers metadata comments, @set vars + ${var} substitution, step lines with
JSON-literal params, reserved keyword lifting, trailing -> $captures and
key="value" overrides, balanced-paren parsing, on_fail defaults, and the
NexusParseError line_no contract on malformed input.
"""

from __future__ import annotations

import pytest

from nexus_common.parser import NexusParseError, parse_nexus_string


# ── Metadata comments ────────────────────────────────────────────────────


def test_metadata_name_pool_node():
    """`# key: value` header comments populate name / _pool_name / _node_id.

    These three drive job naming and scheduler targeting, so they must be lifted
    out of the comment stream rather than ignored like ordinary comments.
    """
    src = """
    # name: smoke test
    # pool: gpu-pool
    # node: abc-123-uuid
    run_python("code": "x")
    """
    out = parse_nexus_string(src)
    assert out["name"] == "smoke test"
    assert out["_pool_name"] == "gpu-pool"
    assert out["_node_id"] == "abc-123-uuid"


def test_metadata_keys_lowercased():
    # The regex captures the key verbatim then lowercases it.
    """Metadata keys are case-insensitive (`# Name:` == `# name:`).

    Hand-written .nexus files capitalize inconsistently; normalizing at parse time
    keeps the downstream dict keys stable.
    """
    out = parse_nexus_string("# Name: hello")
    assert out["name"] == "hello"


def test_metadata_value_may_contain_colons():
    # The value capture is greedy-to-end, so embedded colons (e.g. a URL) survive.
    """Only the FIRST colon splits key from value, so URLs survive intact.

    A naive split(':') would truncate `http://host:8080/path` to `http`.
    """
    out = parse_nexus_string("# name: http://host:8080/path")
    assert out["name"] == "http://host:8080/path"


def test_metadata_absent_yields_none():
    """The metadata keys always exist in the result, defaulting to None.

    A stable output shape means callers can read out['_pool_name'] unconditionally
    instead of guarding with .get().
    """
    out = parse_nexus_string('run_python("code": "x")')
    assert out["name"] is None
    assert out["_pool_name"] is None
    assert out["_node_id"] is None


def test_non_metadata_comment_is_ignored():
    # A "#" line that doesn't match key: value shape is just skipped.
    """A `#` line without a `key: value` shape is skipped, not treated as an error.

    Free-form comments are legal in .nexus files; rejecting them would break real
    user scripts.
    """
    src = """
    # this is a free-form comment without a colon key
    run_python("code": "x")
    """
    out = parse_nexus_string(src)
    assert out["name"] is None
    assert len(out["steps"]) == 1


def test_blank_lines_ignored():
    """Blank lines are skipped and produce no steps."""
    out = parse_nexus_string("\n\n\n# name: n\n\n")
    assert out["name"] == "n"
    assert out["steps"] == []


# ── @set vars + ${var} substitution ──────────────────────────────────────


def test_set_var_substituted_into_step_param():
    """An @set variable is substituted into ${var} references in later step params.

    This is the DSL's only variable mechanism; substitution happens at parse time,
    before the job is stored.
    """
    src = """
    @set("repo": "https://example.com/x.git")
    git_clone("url": "${repo}")
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["url"] == "https://example.com/x.git"


def test_unknown_var_left_intact():
    # ${clone_dir} was never @set, so it survives for the runner to resolve.
    """An unresolved ${var} is left verbatim for the runner to fill in later.

    Critical: runtime captures (e.g. ${clone_dir} from a `-> $clone_dir`) are NOT
    known at parse time. Erroring or blanking them here would break every chained
    job.
    """
    src = 'run_python("path": "${clone_dir}/main.py")'
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["path"] == "${clone_dir}/main.py"


def test_var_substitution_inside_list():
    """Substitution recurses into list values, leaving unknown vars untouched."""
    src = """
    @set("a": "one")
    run_python("items": ["${a}", "${b}", "static"])
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["items"] == ["one", "${b}", "static"]


def test_var_substitution_inside_nested_dict():
    """Substitution recurses into arbitrarily nested dict values.

    Also covers partial substitution inside a larger string ("${host}:22").
    """
    src = """
    @set("host": "myhost")
    run_python("cfg": {"server": {"addr": "${host}:22"}})
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["cfg"]["server"]["addr"] == "myhost:22"


def test_set_var_references_earlier_set_var():
    # @set values can reference previously-declared @set vars.
    """A later @set line may reference a variable defined by an earlier @set line.

    Substitution is applied in declaration order, which is what lets users build
    paths incrementally.
    """
    src = """
    @set("base": "/data")
    @set("full": "${base}/out")
    run_python("p": "${full}")
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["p"] == "/data/out"


def test_set_within_same_line_references_prior_key():
    # _parse_set_literal threads {**set_vars, **out} so a later key on the
    # same @set line sees an earlier key from the same line.
    """Within one @set line, a later key can reference an earlier key from that line.

    Implementation detail this pins: _parse_set_literal threads
    ``{**set_vars, **out}`` while walking the literal, so intra-line references
    resolve left-to-right.
    """
    src = '@set("a": "X", "b": "${a}Y")'
    out = parse_nexus_string(src + '\nrun_python("v": "${b}")')
    assert out["steps"][0]["params"]["v"] == "XY"


def test_set_value_preserves_unknown_var():
    # An unknown ${var} inside an @set value is left intact (it can still be
    # resolved later by the runner once it flows into a step param).
    """An unknown ${var} inside an @set value stays unresolved through to the step.

    Same rationale as unknown vars in params: the runner may resolve it later from
    a runtime capture.
    """
    src = """
    @set("a": "${nope}/x")
    run_python("v": "${a}")
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["v"] == "${nope}/x"


def test_non_string_var_left_unchanged():
    # ${...} substitution only applies to strings; numbers pass through.
    """Substitution only touches strings; ints/bools pass through with their JSON type.

    Coercing everything to str would break params with numeric/boolean schemas.
    """
    src = """
    @set("n": "ignored")
    run_python("count": 42, "flag": true)
    """
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["count"] == 42
    assert out["steps"][0]["params"]["flag"] is True


# ── Step lines with JSON-literal params ───────────────────────────────────


def test_json_literal_param_types():
    """Param values are decoded as real JSON, preserving every scalar and container type.

    Step PARAMS_SCHEMA validation is type-sensitive, so `7` must stay an int and
    `false` must stay a bool.
    """
    src = (
        'run_python("s": "str", "i": 7, "f": 1.5, "b": false, '
        '"nul": null, "lst": [1, 2], "obj": {"k": "v"})'
    )
    out = parse_nexus_string(src)
    p = out["steps"][0]["params"]
    assert p["s"] == "str"
    assert p["i"] == 7
    assert p["f"] == 1.5
    assert p["b"] is False
    assert p["nul"] is None
    assert p["lst"] == [1, 2]
    assert p["obj"] == {"k": "v"}


def test_empty_params_body():
    """`step()` with no arguments yields an empty params dict, not an error."""
    out = parse_nexus_string("noop()")
    assert out["steps"][0]["step"] == "noop"
    assert out["steps"][0]["params"] == {}


def test_step_name_recorded():
    """The identifier before the paren becomes the step name used for registry lookup."""
    out = parse_nexus_string('git_clone("url": "u")')
    assert out["steps"][0]["step"] == "git_clone"


def test_multiple_steps_in_order():
    """Steps are emitted in source order.

    Order is the execution order and the index space that `jump` targets, so any
    reordering would silently retarget jumps.
    """
    src = """
    a("x": 1)
    b("y": 2)
    c("z": 3)
    """
    out = parse_nexus_string(src)
    assert [s["step"] for s in out["steps"]] == ["a", "b", "c"]


# ── Reserved keyword lifting ──────────────────────────────────────────────


def test_reserved_keywords_lifted_out_of_params():
    """Reserved keys are lifted onto the step record and removed from params.

    on_fail / target_os / target_node_id / target_pool_id are runner and scheduler
    directives, not step inputs. Leaving them in params would trip the step's
    "unknown parameter" validation pass.
    """
    src = (
        'git_clone("url": "u", "on_fail": "continue", "target_os": "linux", '
        '"target_node_id": "n1", "target_pool_id": "p1")'
    )
    out = parse_nexus_string(src)
    step = out["steps"][0]
    assert step["on_fail"] == "continue"
    assert step["target_os"] == "linux"
    assert step["target_node_id"] == "n1"
    assert step["target_pool_id"] == "p1"
    # Reserved keys must NOT leak into params.
    assert step["params"] == {"url": "u"}


def test_default_on_fail_is_stop():
    """on_fail defaults to 'stop'.

    Fail-safe default: an unannotated failing step halts the job rather than
    silently continuing with a broken context.
    """
    out = parse_nexus_string('run_python("code": "x")')
    assert out["steps"][0]["on_fail"] == "stop"


def test_non_reserved_keys_stay_in_params():
    """Ordinary keys are left in params untouched."""
    out = parse_nexus_string('run_python("code": "x", "timeout": 30)')
    assert out["steps"][0]["params"] == {"code": "x", "timeout": 30}


# ── Trailing -> $captures ─────────────────────────────────────────────────


def test_single_capture():
    """A trailing `-> $name` records one capture on the step record.

    Captures name the outputs this step publishes into the downstream context.
    """
    out = parse_nexus_string('git_clone("url": "u") -> $clone_dir')
    assert out["steps"][0]["_captures"] == ["clone_dir"]


def test_multiple_captures_comma_separated():
    """Multiple comma-separated `$captures` are all recorded, in order."""
    out = parse_nexus_string('build("x": 1) -> $a, $b, $c')
    assert out["steps"][0]["_captures"] == ["a", "b", "c"]


def test_no_captures_key_when_absent():
    """Steps without a `->` clause omit _captures entirely (rather than storing []).

    Downstream code distinguishes "no capture clause" from "empty capture list".
    """
    out = parse_nexus_string('run_python("code": "x")')
    assert "_captures" not in out["steps"][0]


def test_captures_plus_trailing_keyword():
    """A `-> $capture` clause and a trailing key="value" override can coexist on one line."""
    out = parse_nexus_string('git_clone("url": "u") -> $d on_fail="continue"')
    step = out["steps"][0]
    assert step["_captures"] == ["d"]
    assert step["on_fail"] == "continue"


# ── Trailing key="value" overrides ────────────────────────────────────────


def test_trailing_keyword_override():
    """A trailing `key="value"` sets a reserved field on the step record."""
    out = parse_nexus_string('run_python("code": "x") on_fail="continue"')
    assert out["steps"][0]["on_fail"] == "continue"


def test_trailing_override_beats_in_params():
    # In-params on_fail is "stop"; trailing override wins -> "continue".
    """The trailing form wins over the same key given inside the params body.

    Precedence matters: the trailing position is the more specific / more visible
    one, so it must be applied last.
    """
    src = 'run_python("code": "x", "on_fail": "stop") on_fail="continue"'
    out = parse_nexus_string(src)
    assert out["steps"][0]["on_fail"] == "continue"


def test_multiple_trailing_keywords():
    """Several trailing key="value" pairs on one line are all applied."""
    out = parse_nexus_string(
        'run_python("code": "x") on_fail="continue" target_os="macos"'
    )
    step = out["steps"][0]
    assert step["on_fail"] == "continue"
    assert step["target_os"] == "macos"


def test_trailing_target_os_override_beats_in_params():
    # A reserved key other than on_fail can also be overridden by the trailing form.
    """Trailing-wins precedence applies to every reserved key, not just on_fail."""
    src = 'run_python("code": "x", "target_os": "linux") target_os="macos"'
    out = parse_nexus_string(src)
    assert out["steps"][0]["target_os"] == "macos"


def test_trailing_arbitrary_keyword_lands_on_step_record():
    # _parse_trailing_keywords does not filter to reserved keys: any key="value"
    # in the trailing position is written straight onto the step record (not params).
    """Trailing keywords are NOT filtered to the reserved set.

    Any key="value" is written straight onto the step record. Documented as
    deliberate: it keeps the parser forward-compatible with new runner directives,
    but it also means a typo'd reserved key is silently accepted rather than
    rejected. It must still never leak into params.
    """
    out = parse_nexus_string('run_python("code": "x") foo="bar"')
    step = out["steps"][0]
    assert step["foo"] == "bar"
    # It must NOT leak into params.
    assert step["params"] == {"code": "x"}


def test_arrow_followed_by_non_capture_keeps_no_captures():
    # `-> notacapture` has no `$name` token, so the capture regex doesn't match
    # and no _captures key is produced.
    """`-> something` without a `$` sigil matches no capture and produces no _captures.

    The capture regex requires the sigil, so a malformed arrow clause is dropped
    rather than capturing a bogus name.
    """
    out = parse_nexus_string('run_python("code": "x") -> notacapture')
    assert "_captures" not in out["steps"][0]


def test_capture_with_trailing_comma():
    # A dangling comma after the last $capture is tolerated.
    """A dangling comma after the last $capture is tolerated."""
    out = parse_nexus_string('build("x": 1) -> $a,')
    assert out["steps"][0]["_captures"] == ["a"]


# ── Balanced-paren parsing ────────────────────────────────────────────────


def test_nested_parens_inside_string_value():
    # Parens inside a quoted string must not affect depth tracking.
    """Parens inside a quoted string do not affect the balanced-paren depth counter.

    Without in-string tracking, `print((1 + 2))` would close the step body early.
    """
    out = parse_nexus_string('run_python("code": "print((1 + 2))")')
    assert out["steps"][0]["params"]["code"] == "print((1 + 2))"


def test_close_paren_inside_string_does_not_end_body():
    """A `)` inside a string does not terminate the params body.

    The most common real-world break: a shell/python snippet containing a paren.
    """
    out = parse_nexus_string('run_python("code": "a) still inside", "y": 1)')
    p = out["steps"][0]["params"]
    assert p["code"] == "a) still inside"
    assert p["y"] == 1


def test_single_and_double_quotes_in_string_tracking():
    # A single quote inside a double-quoted JSON string is just a char.
    """A single quote inside a double-quoted JSON string is an ordinary character.

    The scanner must track the *opening* quote character, not toggle on any quote.
    """
    out = parse_nexus_string('run_python("code": "it\'s fine")')
    assert out["steps"][0]["params"]["code"] == "it's fine"


def test_escaped_quote_inside_string():
    # Backslash-escaped quote stays inside the string for both paren
    # tracking and JSON decoding.
    """A backslash-escaped quote keeps the string open for both paren tracking and JSON decoding."""
    out = parse_nexus_string('run_python("code": "say \\"hi\\"")')
    assert out["steps"][0]["params"]["code"] == 'say "hi"'


# ── NexusParseError + line_no contract ────────────────────────────────────


def test_unterminated_paren_raises_with_line_no():
    """An unclosed step body raises NexusParseError pointing at the step's own line.

    line_no is what the UI highlights, so it must be the line the call STARTS on
    (3 here), not the last line scanned.
    """
    src = """
    # name: n
    run_python("code": "x"
    """
    # The step call spans line 3 (after the leading blank line).
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 3
    assert "unterminated" in str(exc.value)


def test_malformed_set_raises_with_line_no():
    # Missing colon -> json.loads fails inside @set.
    """A malformed @set literal raises with the offending line number and an 'invalid @set' detail."""
    src = """
    @set("broken" "no colon")
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 2
    assert "invalid @set" in exc.value.detail


def test_set_non_string_value_raises():
    # @set values must be strings; a numeric value is rejected.
    """@set values must be strings; a numeric value is rejected.

    Substitution is textual, so a non-string value could not be spliced into a
    ${var} reference.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@set("n": 5)')
    assert exc.value.line_no == 1
    assert "must be a string" in str(exc.value)


def test_malformed_step_head_raises():
    # A line that isn't a comment / @set / step_name(...) is an invalid step.
    """A line that is neither comment, @set, nor `name(...)` raises 'invalid step'."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("not a valid line at all")
    assert exc.value.line_no == 1
    assert "invalid step" in exc.value.detail


def test_step_with_bad_json_body_raises():
    # Body wrapped in {...} then json.loads -> trailing comma is invalid JSON.
    """A params body that isn't valid JSON (e.g. trailing comma) raises 'invalid step'.

    The body is wrapped in {...} and handed to json.loads, so JSON's strictness
    (no trailing commas, double quotes only) is the DSL's strictness.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("code": "x",)')
    assert exc.value.line_no == 1
    assert "invalid step" in exc.value.detail


def test_error_message_includes_line_prefix():
    # NexusParseError str() is prefixed with "line N:".
    """NexusParseError renders as 'line N: detail' and exposes line_no/detail separately.

    The API returns both the formatted string and the structured fields, so both
    halves of the contract are asserted.
    """
    err = NexusParseError(7, "boom")
    assert str(err) == "line 7: boom"
    assert err.line_no == 7
    assert err.detail == "boom"


def test_line_no_accounts_for_preceding_lines():
    """line_no counts every source line, including comments and @set lines.

    Off-by-one here would point the editor's error marker at the wrong line.
    """
    src = "# name: n\n@set(\"a\": \"1\")\nrun_python(\"code\": \"x\"\n"
    # The unterminated step is on line 3.
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 3
