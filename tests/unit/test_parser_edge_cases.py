"""Adversarial / malformed-input tests for the .nexus DSL parser.

Companion to ``test_parser.py``, which covers the happy paths and the basic
error contract. This module attacks the parser instead: unbalanced quotes and
parens, unicode (identifiers, escapes, exotic line separators, BOM), 200k-char
lines, empty and whitespace-only sources, illegal step names, duplicate
captures and duplicate JSON keys, malformed JSON literal bodies, comment
edge cases, CRLF/CR line endings, and — for every failure mode — the exact
``NexusParseError`` message text and 1-based ``line_no``.

Several tests here deliberately pin *permissive* behavior (a BOM'd file, a
lone surrogate, an extra ``)``, a ``@setx`` directive). They are documentation
of what the parser actually does today so that a future change to the grammar
is a visible, intentional test change rather than a silent one.
"""

from __future__ import annotations

import math

import pytest

from nexus_common.parser import NexusParseError, parse_nexus_string


# ── Empty and whitespace-only input ──────────────────────────────────────


def test_empty_string_yields_empty_job():
    """An empty source parses to the full result shape with no steps.

    The CLI hands whatever the user saved straight to the parser; an empty file
    must not raise, or "nexus submit empty.nexus" would crash instead of
    reporting a job with no steps.
    """
    out = parse_nexus_string("")
    assert out == {"name": None, "_pool_name": None, "_node_id": None, "steps": []}


def test_spaces_and_tabs_only_yields_empty_job():
    """A source of only spaces and tabs is skipped by the blank-line guard.

    ``line.strip()`` runs before any classification, so indentation-only lines
    must never reach the step parser and be reported as "invalid step".
    """
    out = parse_nexus_string("   \t  ")
    assert out["steps"] == []
    assert out["name"] is None


def test_whitespace_and_newline_mix_yields_empty_job():
    """Interleaved blank / whitespace-only lines all collapse to zero steps."""
    out = parse_nexus_string("\n  \n\t\n   \n")
    assert out["steps"] == []


def test_crlf_only_input_yields_empty_job():
    """A CRLF-only source is empty, i.e. the stray ``\\r`` never becomes a step.

    ``splitlines`` consumes ``\\r\\n`` as one break; if it did not, each ``\\r``
    would survive ``strip()``... it does not, and this pins that.
    """
    out = parse_nexus_string("\r\n\r\n")
    assert out["steps"] == []


def test_lone_carriage_return_only_input_yields_empty_job():
    """A classic-Mac source of a single bare ``\\r`` yields no steps."""
    out = parse_nexus_string("\r")
    assert out["steps"] == []


# ── Comment handling ─────────────────────────────────────────────────────


def test_metadata_without_space_after_hash():
    """``#name: x`` (no space) is still metadata; the space after ``#`` is optional."""
    out = parse_nexus_string("#name: x")
    assert out["name"] == "x"


def test_metadata_tolerates_padding_around_key_and_colon():
    """Whitespace around the key, the colon, and the value is all stripped.

    Hand-aligned headers (``#   name   :   x``) are common in real .nexus files;
    the value must come back trimmed so it is not stored with padding.
    """
    out = parse_nexus_string("#   name   :   x   ")
    assert out["name"] == "x"


def test_metadata_with_empty_value_is_not_recorded():
    """``# name:`` with nothing after the colon does not match and is dropped.

    The value group requires at least one character, so an unfinished header
    leaves ``name`` as None rather than storing an empty job name.
    """
    out = parse_nexus_string("# name:")
    assert out["name"] is None


def test_metadata_with_whitespace_only_value_is_not_recorded():
    """``# name: `` (trailing spaces only) yields None, never an empty-string name.

    The whole line is ``strip()``ed before the regex runs, so the trailing space
    is gone and the value group finds nothing to match. Pinned because an
    empty-string job name and a None job name are handled differently downstream.
    """
    out = parse_nexus_string("# name:    ")
    assert out["name"] is None


def test_double_hash_line_is_not_metadata():
    """``## name: x`` is a plain comment because the key must start with a word char."""
    out = parse_nexus_string("## name: x")
    assert out["name"] is None


def test_hash_only_line_is_ignored():
    """A lone ``#`` separator line is skipped and does not affect the steps."""
    out = parse_nexus_string('#\nrun("a": 1)')
    assert out["name"] is None
    assert len(out["steps"]) == 1


def test_later_metadata_line_overwrites_earlier_one():
    """Repeating a metadata key keeps the last occurrence.

    ``metadata[key] = value`` overwrites, so a duplicated ``# name:`` is not an
    error — the bottom-most one wins.
    """
    out = parse_nexus_string("# name: a\n# name: b")
    assert out["name"] == "b"


def test_unrecognized_metadata_key_is_dropped_silently():
    """An unknown ``# key: value`` header parses cleanly and is then discarded.

    Only name/pool/node are read out of the metadata dict, so a typo'd or
    future header never fails the parse (documented sharp edge in the module
    docstring).
    """
    out = parse_nexus_string('# my-key: v\nrun("a": 1)')
    assert out["name"] is None
    assert out["_pool_name"] is None
    assert len(out["steps"]) == 1


def test_non_ascii_metadata_key_does_not_alias_name():
    """``# näme:`` is a distinct (and therefore ignored) key, not a fuzzy ``name``.

    The key charset uses ``\\w``, which matches non-ASCII letters, so the header
    parses — but lowercasing "näme" never equals "name", so no job name is set.
    """
    out = parse_nexus_string("# n\u00e4me: x")
    assert out["name"] is None


def test_metadata_value_preserves_decomposed_unicode():
    """A metadata value is stored byte-for-byte, with no unicode normalization.

    "cafe" + COMBINING ACUTE must not be silently NFC-folded, or a job name
    would stop round-tripping against what the user typed.
    """
    out = parse_nexus_string("# name: cafe\u0301")
    assert out["name"] == "cafe\u0301"
    assert out["name"] != "caf\u00e9"


def test_hash_inside_param_string_is_not_a_comment():
    """A ``#`` inside a quoted param value is data, not the start of a comment.

    Comment detection is anchored to the start of the stripped line, so shell
    comments and Python comments inside a ``run_python`` snippet survive.
    """
    out = parse_nexus_string('run_python("code": "# not a comment")')
    assert out["steps"][0]["params"]["code"] == "# not a comment"


def test_trailing_text_after_step_call_is_ignored():
    """Free text after the closing paren is silently discarded.

    ``_parse_trailing_keywords`` uses ``finditer``, so anything that is not
    ``key="value"`` is skipped — which is what makes an end-of-line comment
    work at all.
    """
    out = parse_nexus_string('run("a": 1) # explanatory note')
    assert out["steps"][0]["params"] == {"a": 1}
    assert out["steps"][0]["on_fail"] == "stop"


def test_key_value_inside_trailing_comment_is_still_applied():
    """POSSIBLE SHARP EDGE: ``key="value"`` inside a trailing comment is honoured.

    There is no comment stripping in the trailing region, so a commented-out
    override still lands on the step record. Pinned as current behavior.
    """
    out = parse_nexus_string('run("a": 1) # was: on_fail="continue"')
    assert out["steps"][0]["on_fail"] == "continue"


def test_metadata_line_without_hash_is_an_invalid_step():
    """A header written without the leading ``#`` falls through to the step parser."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("name: x")
    assert exc.value.line_no == 1
    assert "invalid step" in exc.value.detail
    assert repr("name: x") in exc.value.detail


def test_var_reference_in_metadata_is_not_substituted():
    """``${var}`` is never expanded inside a metadata comment.

    Substitution runs only on decoded step params and ``@set`` values, so a job
    name keeps the literal placeholder.
    """
    src = '@set("a": "X")\n# name: ${a}\nrun("b": 1)'
    out = parse_nexus_string(src)
    assert out["name"] == "${a}"


# ── Unbalanced / nested quotes and parens ────────────────────────────────


def test_open_paren_with_nothing_after_it_reports_unterminated():
    """``run(`` alone raises the unterminated-paren error on its own line."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("run(")
    assert exc.value.line_no == 1
    assert exc.value.detail == "invalid step: unterminated '(' in step call"


def test_odd_number_of_quotes_swallows_the_closing_paren():
    """An unbalanced quote leaves the scanner inside a string, so ``)`` never closes.

    ``run_python("code": "x"")`` opens a third string whose content is the
    remaining ``)``; the reported error is "unterminated '('", not a JSON error.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("code": "x"")')
    assert exc.value.line_no == 1
    assert "unterminated '('" in exc.value.detail


def test_unterminated_single_quote_swallows_the_closing_paren():
    """A stray ``'`` also opens string state, since the scanner tracks both quote chars."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("run_python(\"code\": \"x\", 'oops)")
    assert exc.value.line_no == 1
    assert "unterminated '('" in exc.value.detail


def test_closing_paren_before_opening_paren_is_a_bad_step_head():
    """``run)a(`` fails the head regex, which requires ``(`` right after the name."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("run)a(")
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_extra_closing_paren_after_body_is_silently_ignored():
    """POSSIBLE SHARP EDGE: a surplus ``)`` after the body is accepted.

    The scanner returns as soon as depth hits zero and the leftover ``)`` falls
    into the trailing region, where non-keyword text is dropped. So a typo like
    ``run("a": 1))`` parses instead of erroring.
    """
    out = parse_nexus_string('run("a": 1))')
    assert out["steps"][0]["step"] == "run"
    assert out["steps"][0]["params"] == {"a": 1}


def test_nested_parens_in_body_are_captured_then_rejected_by_json():
    """Nested parens do not break the scanner; they break ``json.loads`` instead.

    Depth tracking correctly matches the *outer* paren, so the body handed to
    JSON is ``("a": 1)`` and the reported error is a JSON one — proof the two
    stages are separate.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run(("a": 1))')
    assert exc.value.line_no == 1
    assert "Expecting property name enclosed in double quotes" in exc.value.detail


def test_unterminated_paren_on_a_very_long_line_still_reports_line_one():
    """A 50k-character unterminated line reports line 1, not a truncated position.

    The scanner walks to end-of-line before raising; the error is anchored to the
    source line, never to the character offset.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("code": "' + "y" * 50_000)
    assert exc.value.line_no == 1
    assert "unterminated '('" in exc.value.detail


def test_backslash_outside_a_string_reaches_json_and_is_rejected():
    """A stray backslash outside a quoted string is not treated as an escape.

    The scanner only honours escapes while in string state, so the backslash
    stays in the body text and ``json.loads`` rejects it.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python(\\ "a": 1)')
    assert exc.value.line_no == 1
    assert "Expecting property name enclosed in double quotes" in exc.value.detail


def test_escaped_backslash_in_string_value_survives():
    """A JSON-escaped backslash decodes to one backslash (Windows paths work).

    Both the paren scanner (which must not treat ``\\\\`` as escaping the quote)
    and ``json.loads`` have to agree here.
    """
    out = parse_nexus_string('run("d": "C:\\\\tmp\\\\x")')
    assert out["steps"][0]["params"]["d"] == "C:\\tmp\\x"


def test_space_between_step_name_and_open_paren_is_allowed():
    """``run_python ("code": "x")`` parses; the head regex permits ``\\s*`` before ``(``."""
    out = parse_nexus_string('run_python ("code": "x")')
    assert out["steps"][0]["step"] == "run_python"
    assert out["steps"][0]["params"] == {"code": "x"}


def test_whitespace_only_param_body_yields_empty_params():
    """``run(   )`` is equivalent to ``run()`` — the body is stripped before JSON.

    Without the strip, ``json.loads("{   }")`` would still work, but the guard
    also protects the empty-string case; this pins the whitespace variant.
    """
    out = parse_nexus_string("run(   )")
    assert out["steps"][0]["params"] == {}


def test_deeply_nested_json_list_body_parses():
    """A 200-deep nested list body parses without blowing the recursion limit.

    ``_substitute_vars`` recurses per container level, so deep-but-legal JSON is
    a real risk of RecursionError; this pins that 200 levels is safe.
    """
    depth = 200
    src = 'run("a": ' + "[" * depth + "1" + "]" * depth + ")"
    value = parse_nexus_string(src)["steps"][0]["params"]["a"]
    for _ in range(depth):
        assert isinstance(value, list)
        value = value[0]
    assert value == 1


# ── Unicode ──────────────────────────────────────────────────────────────


def test_unicode_param_value_is_preserved_verbatim():
    """Accented, CJK, and astral-plane characters survive parsing unchanged."""
    out = parse_nexus_string('run_python("code": "h\u00e9llo \u65e5\u672c\u8a9e \U0001f389")')
    assert out["steps"][0]["params"]["code"] == "h\u00e9llo \u65e5\u672c\u8a9e \U0001f389"


def test_json_unicode_escape_is_decoded():
    """A ``\\uXXXX`` escape in a param string is decoded by ``json.loads``.

    Users on ASCII-only editors rely on escapes, so the decoded form (not the
    literal backslash-u text) must reach the step params.
    """
    out = parse_nexus_string('run_python("code": "\\u00e9")')
    assert out["steps"][0]["params"]["code"] == "\u00e9"


def test_json_surrogate_pair_is_decoded_to_one_astral_char():
    """A ``\\udXXX\\udYYY`` surrogate pair decodes to a single non-BMP character."""
    out = parse_nexus_string('run_python("code": "\\ud83d\\ude80")')
    assert out["steps"][0]["params"]["code"] == "\U0001f680"


def test_lone_surrogate_escape_is_accepted():
    """POSSIBLE SHARP EDGE: an unpaired surrogate escape parses successfully.

    ``json.loads`` permits it, so the parser emits a string that cannot be
    UTF-8 encoded. Pinned as current behavior; the failure would surface later,
    at serialization time, rather than at parse time.
    """
    out = parse_nexus_string('run_python("code": "\\ud83d")')
    assert out["steps"][0]["params"]["code"] == "\ud83d"


def test_nul_escape_is_accepted_in_a_param_value():
    """POSSIBLE SHARP EDGE: ``\\u0000`` decodes to an embedded NUL and is accepted.

    Legal JSON, but hostile to anything that later hands the value to a C-string
    API (subprocess argv, SQLite TEXT). Pinned as current behavior.
    """
    out = parse_nexus_string('run_python("code": "a\\u0000b")')
    assert out["steps"][0]["params"]["code"] == "a\x00b"


def test_step_name_may_contain_non_ascii_word_characters():
    """POSSIBLE SHARP EDGE: ``st\u00e9p(...)`` is accepted as a step name.

    The head regex is ``[A-Za-z_][\\w]*`` and Python's ``\\w`` is unicode-aware,
    so only the FIRST character is restricted to ASCII. The name is passed to
    the registry lookup as-is and will fail there, not here.
    """
    out = parse_nexus_string('st\u00e9p("a": 1)')
    assert out["steps"][0]["step"] == "st\u00e9p"


def test_step_name_starting_with_non_ascii_is_rejected():
    """A step name whose first character is non-ASCII fails the head regex."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('\u65e5\u672c("a": 1)')
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_emoji_step_name_is_rejected():
    """An emoji is not a ``\\w`` character either, so it cannot start a step call."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('\U0001f389("a": 1)')
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_set_variable_name_may_be_non_ascii():
    """A non-ASCII ``@set`` name substitutes correctly, because ``_VAR_REF`` uses ``\\w``.

    Pins that declaration and reference use the same charset — a mismatch would
    leave the reference unexpanded and silently ship ``${v\u00e4r}`` to the runner.
    """
    src = '@set("v\u00e4r": "ok")\nrun("v": "${v\u00e4r}")'
    out = parse_nexus_string(src)
    assert out["steps"][0]["params"]["v"] == "ok"


def test_capture_name_may_be_non_ascii():
    """A ``-> $caf\u00e9`` capture is recorded with its non-ASCII characters intact."""
    out = parse_nexus_string('run("x": 1) -> $caf\u00e9')
    assert out["steps"][0]["_captures"] == ["caf\u00e9"]


def test_utf8_bom_makes_the_first_line_an_invalid_step():
    """POSSIBLE BUG: a UTF-8 BOM turns line 1 into "invalid step".

    U+FEFF is not whitespace, so ``strip()`` keeps it and the line no longer
    starts with ``#``. A .nexus file saved by a BOM-writing editor fails with a
    confusing error that does not mention encoding. Pinned as current behavior.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("\ufeff# name: x")
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail
    assert "\\ufeff" in exc.value.detail


def test_non_breaking_space_indent_is_stripped():
    """A U+00A0 indent IS stripped, because Python treats it as whitespace.

    Contrast with the BOM case above: the two look identical in an editor but
    behave differently, so both are pinned.
    """
    out = parse_nexus_string("\u00a0# name: x")
    assert out["name"] == "x"


def test_unicode_line_separator_splits_a_step_line():
    """POSSIBLE SHARP EDGE: U+2028 acts as a line break inside a quoted string.

    ``str.splitlines`` treats LINE SEPARATOR as a newline, so a param value
    containing one is cut in half and the step reports an unterminated paren on
    the line where it started.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("code": "a\u2028b")')
    assert exc.value.line_no == 1
    assert "unterminated '('" in exc.value.detail


def test_unicode_line_separator_splits_two_metadata_headers():
    """Two headers joined by U+2028 are parsed as two separate metadata lines.

    Confirms the split above is ``splitlines`` semantics and not a quirk of the
    step path — and that reported line numbers can disagree with an editor that
    does not treat U+2028 as a break.
    """
    out = parse_nexus_string("# name: a\u2028# pool: b")
    assert out["name"] == "a"
    assert out["_pool_name"] == "b"


def test_next_line_control_char_splits_lines():
    """U+0085 (NEL) is also a ``splitlines`` break, so it separates headers."""
    out = parse_nexus_string("# name: a\x85# pool: b")
    assert out["name"] == "a"
    assert out["_pool_name"] == "b"


def test_form_feed_splits_lines():
    """A form feed (\\x0c) is a ``splitlines`` break too, not ordinary whitespace."""
    out = parse_nexus_string("# name: a\x0c# pool: b")
    assert out["name"] == "a"
    assert out["_pool_name"] == "b"


def test_vertical_tab_splits_lines():
    """A vertical tab (\\x0b) likewise ends the line."""
    out = parse_nexus_string("# name: a\x0b# pool: b")
    assert out["name"] == "a"
    assert out["_pool_name"] == "b"


# ── Very long lines and large sources ────────────────────────────────────


def test_very_long_string_param_value_parses_intact():
    """A 200k-character param value round-trips with no truncation.

    ``run_python`` bodies are real user code and can be large; the hand-rolled
    scanner is O(n) per line and must not cap length.
    """
    big = "z" * 200_000
    out = parse_nexus_string('run_python("code": "%s")' % big)
    assert out["steps"][0]["params"]["code"] == big


def test_two_thousand_params_on_one_line_all_parse():
    """A body with 2000 key/value pairs yields all 2000 params.

    Guards against any accidental regex-based body parse that would only match
    the first pair.
    """
    body = ", ".join(f'"k{i}": {i}' for i in range(2000))
    out = parse_nexus_string(f"run({body})")
    params = out["steps"][0]["params"]
    assert len(params) == 2000
    assert params["k0"] == 0
    assert params["k1999"] == 1999


def test_five_hundred_captures_on_one_line_all_parse():
    """A 500-name ``-> $a, $b, ...`` clause records every capture, in order.

    The capture regex consumes a repeated group; this pins that the whole run is
    consumed rather than just the first token.
    """
    caps = ", ".join(f"$c{i}" for i in range(500))
    out = parse_nexus_string(f'run("a": 1) -> {caps}')
    assert out["steps"][0]["_captures"] == [f"c{i}" for i in range(500)]


def test_thousand_line_source_yields_thousand_steps_in_order():
    """A 1000-line source produces 1000 steps whose order matches the source."""
    src = "\n".join(f'run("i": {i})' for i in range(1000))
    steps = parse_nexus_string(src)["steps"]
    assert len(steps) == 1000
    assert steps[0]["params"]["i"] == 0
    assert steps[999]["params"]["i"] == 999


def test_very_long_metadata_value_is_preserved():
    """A 50k-character metadata value survives the non-greedy value capture.

    The value group is ``.+?`` anchored to end-of-line; this pins that it still
    captures the whole (long) tail rather than backtracking to something short.
    """
    long_name = "n" * 50_000
    out = parse_nexus_string("# name: " + long_name)
    assert out["name"] == long_name


# ── Invalid step names and non-step lines ────────────────────────────────


def test_step_name_starting_with_digit_is_rejected():
    """A leading digit fails the head regex and the offending line is echoed.

    The echoed ``repr`` is what an editor shows the user, so it is asserted
    explicitly.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('1step("a": 1)')
    assert exc.value.line_no == 1
    assert exc.value.detail == 'invalid step: expected `step_name(...)`: \'1step("a": 1)\''


def test_hyphenated_step_name_is_rejected():
    """``my-step(...)`` is rejected: ``-`` is not in the identifier charset.

    Metadata keys DO allow hyphens; step names do not. Pinned so the two
    charsets are not accidentally unified.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('my-step("a": 1)')
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_missing_step_name_is_rejected():
    """A bare ``("a": 1)`` with no identifier is rejected rather than defaulted."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('("a": 1)')
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_bare_underscore_is_a_valid_step_name():
    """``_("a": 1)`` is accepted: ``_`` alone satisfies the identifier rule.

    The parser deliberately does not check the registry, so an unusable name is
    still a syntactically valid step.
    """
    out = parse_nexus_string('_("a": 1)')
    assert out["steps"][0]["step"] == "_"


def test_at_prefixed_directive_other_than_set_is_rejected():
    """``@params(...)`` is not a known directive and fails as a step head.

    ``@set`` is the only ``@`` directive; anything else must error rather than be
    quietly ignored.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@params("a": "b")')
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


def test_at_set_is_matched_by_prefix_so_setx_is_treated_as_set():
    """POSSIBLE BUG: ``@setx(...)`` is dispatched to the ``@set`` handler.

    The dispatch is ``line.startswith("@set")``, not an exact-token match, so any
    directive whose name begins with "set" (``@setup``, ``@settings``) silently
    declares variables. Pinned as current behavior.
    """
    out = parse_nexus_string('@setx("a": "b")\nrun("v": "${a}")')
    assert out["steps"][0]["params"]["v"] == "b"


def test_arrow_clause_without_a_step_call_is_rejected():
    """A dangling ``-> $a`` line has no step head and is rejected."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("-> $a")
    assert exc.value.line_no == 1
    assert "expected `step_name(...)`" in exc.value.detail


# ── @set failure modes ───────────────────────────────────────────────────


def test_set_without_parens_reports_a_leaky_index_error_message():
    """POSSIBLE BUG: bare ``@set`` reports "substring not found".

    ``_parse_set_literal`` calls ``line.index("(")`` unguarded, so the user sees
    ``str.index``'s internal message instead of something like "@set requires
    (...)". Pinned as current behavior because it is the error text a UI shows.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("@set")
    assert exc.value.line_no == 1
    assert exc.value.detail == "invalid @set: substring not found"


def test_set_with_unbalanced_paren_reports_step_call_wording():
    """POSSIBLE BUG: an unterminated ``@set`` says "in step call".

    ``_balanced_paren_body`` is shared with the step path and hardcodes "step
    call" in its message, so a broken ``@set`` line is misdescribed. Pinned as
    current behavior.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@set("a": "b"')
    assert exc.value.line_no == 1
    assert exc.value.detail == "invalid @set: unterminated '(' in step call"


def test_set_with_list_value_is_rejected():
    """A list-valued ``@set`` is rejected: substitution is textual, so only strings work."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@set("a": ["x"])')
    assert exc.value.line_no == 1
    assert exc.value.detail == "invalid @set: @set value for 'a' must be a string"


def test_set_with_null_value_is_rejected():
    """A JSON ``null`` ``@set`` value is rejected with the same string-type message."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@set("a": null)')
    assert exc.value.line_no == 1
    assert "must be a string" in exc.value.detail


def test_set_with_boolean_value_is_rejected():
    """A JSON ``true`` ``@set`` value is rejected too, not coerced to "true"."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('@set("a": true)')
    assert exc.value.line_no == 1
    assert "must be a string" in exc.value.detail


def test_empty_set_declares_nothing_and_does_not_error():
    """``@set()`` is a legal no-op line."""
    out = parse_nexus_string('@set()\nrun("a": 1)')
    assert out["steps"][0]["params"] == {"a": 1}


def test_set_value_referencing_itself_is_left_intact():
    """``@set("a": "${a}")`` leaves the reference unexpanded rather than looping.

    The binding is not visible in ``{**set_vars, **out}`` until after its own
    value is substituted, so self-reference is unresolvable by construction —
    which is exactly what prevents infinite expansion.
    """
    out = parse_nexus_string('@set("a": "${a}")\nrun("v": "${a}")')
    assert out["steps"][0]["params"]["v"] == "${a}"


def test_duplicate_key_on_one_set_line_keeps_the_last_value():
    """A key repeated within one ``@set`` body resolves to the last occurrence.

    ``json.loads`` silently overwrites duplicates, so this is last-wins with no
    warning.
    """
    out = parse_nexus_string('@set("a": "1", "a": "2")\nrun("v": "${a}")')
    assert out["steps"][0]["params"]["v"] == "2"


def test_forward_reference_to_a_later_set_is_left_intact():
    """A step above its ``@set`` keeps the literal ``${var}``.

    Declaration order is significant: forward references are impossible, and the
    unresolved placeholder is passed through for the runner instead of erroring.
    """
    out = parse_nexus_string('run("v": "${a}")\n@set("a": "X")')
    assert out["steps"][0]["params"]["v"] == "${a}"


# ── Duplicate captures and duplicate keys ────────────────────────────────


def test_duplicate_captures_are_not_deduplicated():
    """POSSIBLE SHARP EDGE: ``-> $a, $a`` records "a" twice.

    Captures are appended without a membership check, so a copy-paste mistake
    reaches the runner as a two-element list. Pinned as current behavior.
    """
    out = parse_nexus_string('build("x": 1) -> $a, $a')
    assert out["steps"][0]["_captures"] == ["a", "a"]


def test_duplicate_param_key_keeps_the_last_value():
    """POSSIBLE SHARP EDGE: a repeated param key silently resolves to the last value.

    JSON has no duplicate-key error, so ``run("a": 1, "a": 2)`` yields ``a == 2``
    with no diagnostic. Pinned as current behavior.
    """
    out = parse_nexus_string('run("a": 1, "a": 2)')
    assert out["steps"][0]["params"] == {"a": 2}


def test_duplicate_key_in_a_nested_object_keeps_the_last_value():
    """Last-wins applies at every nesting level, not just the top-level body."""
    out = parse_nexus_string('run("o": {"k": 1, "k": 2})')
    assert out["steps"][0]["params"]["o"] == {"k": 2}


def test_duplicate_trailing_keyword_keeps_the_last_value():
    """A trailing key repeated on one line resolves to the rightmost occurrence.

    ``finditer`` walks left to right writing into the same dict, so the last
    ``target_os=`` wins.
    """
    out = parse_nexus_string('run("a": 1) target_os="linux" target_os="macos"')
    assert out["steps"][0]["target_os"] == "macos"


def test_capture_named_like_a_reserved_keyword_stays_a_capture():
    """``-> $on_fail`` is recorded as a capture and does not touch ``on_fail``.

    The capture list and the reserved-keyword namespace are independent; a
    collision must not clobber the runner directive.
    """
    out = parse_nexus_string('run("a": 1) -> $on_fail')
    assert out["steps"][0]["_captures"] == ["on_fail"]
    assert out["steps"][0]["on_fail"] == "stop"


# ── Malformed JSON literal params ────────────────────────────────────────


def test_single_quoted_body_is_rejected_with_double_quote_message():
    """Single-quoted JSON is rejected; the DSL inherits JSON's double-quote rule.

    The message is asserted because it is the user's only hint about what to fix.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string("run_python('code': 'x')")
    assert exc.value.line_no == 1
    assert "Expecting property name enclosed in double quotes" in exc.value.detail


def test_unquoted_key_is_rejected():
    """A bare (unquoted) key is rejected — this is JSON, not Python kwargs."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python(code: "x")')
    assert exc.value.line_no == 1
    assert "Expecting property name enclosed in double quotes" in exc.value.detail


def test_missing_colon_reports_the_delimiter_message():
    """``"code" "x"`` (colon omitted) reports JSON's ``Expecting ':' delimiter``."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("code" "x")')
    assert exc.value.line_no == 1
    assert "Expecting ':' delimiter" in exc.value.detail


def test_leading_comma_in_body_is_rejected():
    """A leading comma is invalid JSON-object innards and is rejected."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python(, "a": 1)')
    assert exc.value.line_no == 1
    assert "invalid step" in exc.value.detail


def test_python_style_true_literal_is_rejected():
    """``True`` (capitalized) is not JSON; only ``true`` is accepted.

    A very likely mistake for Python authors, so the failure mode is pinned.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("a": True)')
    assert exc.value.line_no == 1
    assert "Expecting value" in exc.value.detail


def test_python_style_none_literal_is_rejected():
    """``None`` is rejected as well; JSON's spelling is ``null``."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run_python("a": None)')
    assert exc.value.line_no == 1
    assert "Expecting value" in exc.value.detail


def test_nan_literal_is_accepted_as_a_float():
    """POSSIBLE SHARP EDGE: ``NaN`` is accepted because ``json.loads`` allows it.

    NaN is not standard JSON, so the resulting payload cannot be re-serialized
    with a strict encoder — the DSL is therefore slightly *more* permissive than
    the JSON API it claims to mirror. Pinned as current behavior.
    """
    out = parse_nexus_string('run_python("x": NaN)')
    assert math.isnan(out["steps"][0]["params"]["x"])


def test_infinity_literal_is_accepted_as_a_float():
    """POSSIBLE SHARP EDGE: ``-Infinity`` is likewise accepted, same rationale as NaN."""
    out = parse_nexus_string('run_python("x": -Infinity)')
    assert out["steps"][0]["params"]["x"] == float("-inf")


def test_raw_tab_inside_a_json_string_is_rejected_as_a_control_character():
    """A literal tab inside a quoted value is rejected; it must be written ``\\t``.

    Strip only touches the line ends, so an interior tab reaches ``json.loads``
    and trips its control-character check.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run("code": "a\tb")')
    assert exc.value.line_no == 1
    assert "Invalid control character at" in exc.value.detail


def test_escaped_newline_inside_a_json_string_is_accepted():
    """``\\n`` written as an escape decodes to a real newline inside one source line.

    This is the supported way to express multi-line code, since the DSL has no
    statement continuation.
    """
    out = parse_nexus_string('run("code": "a\\nb")')
    assert out["steps"][0]["params"]["code"] == "a\nb"


def test_invalid_unicode_escape_is_rejected():
    """``\\uZZZZ`` reports JSON's ``Invalid \\uXXXX escape``."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run("code": "\\uZZZZ")')
    assert exc.value.line_no == 1
    assert "Invalid \\uXXXX escape" in exc.value.detail


def test_json_error_message_column_is_json_relative_not_source_relative():
    """The nested JSON message says "line 1" even when the source line is line 2.

    The body is re-wrapped as a standalone one-line JSON document, so its
    coordinates are body-relative. Only ``NexusParseError.line_no`` is authoritative
    for the source position — worth pinning so nobody "fixes" the wrong number.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('a("x": 1)\nb("y": tru)')
    assert exc.value.line_no == 2
    assert "line 1 column" in exc.value.detail
    assert str(exc.value).startswith("line 2: invalid step:")


# ── Trailing clause sharp edges ──────────────────────────────────────────


def test_unquoted_trailing_value_is_silently_dropped():
    """POSSIBLE SHARP EDGE: ``on_fail=continue`` (no quotes) is ignored.

    The keyword regex requires a double-quoted value, and unmatched text is
    skipped, so the step keeps the default ``stop`` with no warning. Pinned as
    current behavior — a real footgun for a mis-typed override.
    """
    out = parse_nexus_string('run("a": 1) on_fail=continue')
    assert out["steps"][0]["on_fail"] == "stop"


def test_single_quoted_trailing_value_is_silently_dropped():
    """A single-quoted trailing value is dropped for the same reason as an unquoted one."""
    out = parse_nexus_string("run(\"a\": 1) on_fail='continue'")
    assert out["steps"][0]["on_fail"] == "stop"


def test_arrow_with_nothing_after_it_yields_no_captures():
    """A dangling ``->`` at end of line produces no captures and no error."""
    out = parse_nexus_string('run("x": 1) ->')
    assert "_captures" not in out["steps"][0]


def test_space_separated_captures_without_commas_are_accepted():
    """``-> $a $b`` (no commas) captures both; the separator comma is optional."""
    out = parse_nexus_string('run("x": 1) -> $a $b')
    assert out["steps"][0]["_captures"] == ["a", "b"]


def test_second_arrow_clause_is_silently_dropped():
    """POSSIBLE SHARP EDGE: only the first ``->`` run is consumed.

    ``-> $a -> $b`` records only "a"; the second arrow lands in the keyword
    region and is skipped. Pinned as current behavior.
    """
    out = parse_nexus_string('run("x": 1) -> $a -> $b')
    assert out["steps"][0]["_captures"] == ["a"]


def test_capture_name_starting_with_a_digit_is_dropped():
    """``-> $1abc`` matches no capture token, so no ``_captures`` key is produced."""
    out = parse_nexus_string('run("a": 1) -> $1abc')
    assert "_captures" not in out["steps"][0]


def test_capture_name_may_start_with_underscore():
    """``-> $_x9`` is a valid capture name (underscore start, digits allowed after)."""
    out = parse_nexus_string('run("a": 1) -> $_x9')
    assert out["steps"][0]["_captures"] == ["_x9"]


def test_junk_after_captures_is_ignored():
    """Non-keyword garbage following a capture clause is skipped, not rejected."""
    out = parse_nexus_string('run("a": 1) -> $x !!!')
    assert out["steps"][0]["_captures"] == ["x"]


def test_captures_must_precede_trailing_keywords():
    """POSSIBLE SHARP EDGE: a ``->`` clause written AFTER a keyword is ignored.

    ``_split_trailing`` only looks for ``->`` at the very start of the trailing
    text, so ``on_fail="continue" -> $x`` applies the override but loses the
    capture entirely. Pinned as current behavior.
    """
    out = parse_nexus_string('run("a": 1) on_fail="continue" -> $x')
    assert out["steps"][0]["on_fail"] == "continue"
    assert "_captures" not in out["steps"][0]


def test_captures_and_keyword_interleaved_stops_at_the_keyword():
    """``-> $a on_fail="continue" $b`` captures only "a"; ``$b`` after the keyword is lost."""
    out = parse_nexus_string('run("a": 1) -> $a on_fail="continue" $b')
    assert out["steps"][0]["_captures"] == ["a"]
    assert out["steps"][0]["on_fail"] == "continue"


def test_non_string_reserved_value_in_body_is_lifted_without_validation():
    """A reserved key with a non-string value is lifted verbatim (no type check).

    ``on_fail: 5`` becomes ``step["on_fail"] == 5``. The parser deliberately
    validates nothing; ``POST /api/jobs`` is the validation boundary.
    """
    out = parse_nexus_string('run("on_fail": 5)')
    assert out["steps"][0]["on_fail"] == 5
    assert out["steps"][0]["params"] == {}


def test_unknown_on_fail_value_is_not_validated():
    """A nonsense ``on_fail`` value passes the parser untouched.

    Same boundary rationale: enum validation belongs to the schema layer.
    """
    out = parse_nexus_string('run("a": 1) on_fail="banana"')
    assert out["steps"][0]["on_fail"] == "banana"


def test_var_reference_in_trailing_keyword_is_not_substituted():
    """POSSIBLE SHARP EDGE: ``${var}`` in a trailing keyword value is left literal.

    Substitution runs on the decoded params body only, never on the trailing
    region, so ``on_fail="${o}"`` ships the placeholder text. Pinned as current
    behavior — the asymmetry with in-body values is surprising.
    """
    out = parse_nexus_string('@set("o": "continue")\nrun("a": 1) on_fail="${o}"')
    assert out["steps"][0]["on_fail"] == "${o}"


def test_arrow_inside_a_param_string_is_not_a_capture_clause():
    """An ``-> $b`` written inside a quoted param value stays data.

    The trailing region starts after the matched closing paren, so in-body text
    can never be reinterpreted as a capture clause.
    """
    out = parse_nexus_string('run_python("code": "a -> $b")')
    assert out["steps"][0]["params"]["code"] == "a -> $b"
    assert "_captures" not in out["steps"][0]


def test_keyword_syntax_inside_a_param_string_is_not_an_override():
    """``on_fail="continue"`` inside a quoted param value does not become an override."""
    out = parse_nexus_string('run_python("code": "on_fail=\\"continue\\"")')
    step = out["steps"][0]
    assert step["params"]["code"] == 'on_fail="continue"'
    assert step["on_fail"] == "stop"


# ── ${var} substitution edge cases ───────────────────────────────────────


def test_param_key_is_not_var_substituted():
    """POSSIBLE SHARP EDGE: ``${var}`` in a param KEY is not expanded.

    ``_substitute_vars`` rebuilds dicts as ``{k: subst(v)}``, leaving keys alone,
    so ``"${k}": "x"`` produces a literal ``${k}`` parameter name that no step
    schema will accept. Pinned as current behavior.
    """
    out = parse_nexus_string('@set("k": "code")\nrun_python("${k}": "x")')
    assert out["steps"][0]["params"] == {"${k}": "x"}


def test_nested_var_reference_is_substituted_only_once():
    """Substitution is a single pass: ``${${a}}`` expands the inner ref only.

    With ``a == "X"`` the result is the literal ``${X}``, not the value of X.
    Pins the documented single-pass guarantee (which is what makes substitution
    terminate).
    """
    out = parse_nexus_string('@set("a": "X")\nrun("v": "${${a}}")')
    assert out["steps"][0]["params"]["v"] == "${X}"


def test_empty_var_reference_is_left_intact():
    """``${}`` matches no variable name and is preserved verbatim."""
    out = parse_nexus_string('run("v": "${}")')
    assert out["steps"][0]["params"]["v"] == "${}"


def test_var_reference_containing_a_space_is_left_intact():
    """``${a b}`` is not a legal reference and is preserved verbatim."""
    out = parse_nexus_string('@set("a": "X")\nrun("v": "${a b}")')
    assert out["steps"][0]["params"]["v"] == "${a b}"


def test_dollar_without_braces_is_left_intact():
    """A shell-style ``$a`` (no braces) is not a reference and is untouched.

    Important for ``run_python`` / shell snippets that legitimately contain ``$``.
    """
    out = parse_nexus_string('@set("a": "X")\nrun("v": "$a")')
    assert out["steps"][0]["params"]["v"] == "$a"


def test_adjacent_var_references_are_both_substituted():
    """``${a}${b}`` with no separator expands both references."""
    out = parse_nexus_string('@set("a": "1", "b": "2")\nrun("v": "${a}${b}")')
    assert out["steps"][0]["params"]["v"] == "12"


# ── CRLF / CR line endings ───────────────────────────────────────────────


def test_crlf_source_parses_like_the_lf_equivalent():
    """A CRLF-terminated source produces the same result as LF.

    Windows-authored .nexus files must not fail because of the trailing ``\\r``
    (``splitlines`` strips it, and ``strip()`` would too).
    """
    crlf = '# name: n\r\nrun_python("code": "x")\r\n'
    lf = '# name: n\nrun_python("code": "x")\n'
    assert parse_nexus_string(crlf) == parse_nexus_string(lf)


def test_crlf_source_reports_the_correct_error_line_no():
    """With CRLF endings the reported ``line_no`` still counts real lines.

    An error on the third CRLF line reports 3 — the ``\\r`` must not be counted
    as an extra line.
    """
    src = '# name: n\r\n@set("a": "1")\r\nrun_python("code": "x"\r\n'
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 3
    assert "unterminated '('" in exc.value.detail


def test_mixed_crlf_and_lf_endings_in_one_source():
    """A source mixing CRLF and LF endings parses all of its lines."""
    src = '# name: n\r\nrun("a": 1)\nrun("b": 2)\r\n'
    out = parse_nexus_string(src)
    assert out["name"] == "n"
    assert [s["params"] for s in out["steps"]] == [{"a": 1}, {"b": 2}]


def test_lone_cr_endings_are_treated_as_line_breaks():
    """Classic-Mac ``\\r``-only endings are split into separate lines.

    ``splitlines`` handles bare CR, so such a file parses rather than being read
    as one giant (and invalid) line.
    """
    out = parse_nexus_string('# name: n\rrun("a": 1)')
    assert out["name"] == "n"
    assert out["steps"][0]["params"] == {"a": 1}


def test_cr_only_source_reports_the_correct_error_line_no():
    """A bare-CR source still attributes the error to the right logical line."""
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('# name: n\rrun("a": 1)\rbad line')
    assert exc.value.line_no == 3
    assert "expected `step_name(...)`" in exc.value.detail


# ── line_no correctness across the file ──────────────────────────────────


def test_line_no_points_at_a_late_error_line():
    """An error on line 20 of a 20-line file reports 20.

    Comments and ``@set`` lines are counted by ``enumerate`` even though they
    produce no steps, so the count cannot drift.
    """
    src = "\n".join(["# c"] * 10 + ['@set("a": "1")'] * 5 + ['run("x": 1)'] * 4 + ["bad line here"])
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 20
    assert exc.value.detail == "invalid step: expected `step_name(...)`: 'bad line here'"


def test_line_no_counts_blank_lines_between_statements():
    """Skipped blank lines still advance the line counter.

    The ``continue`` for blank lines happens inside the ``enumerate`` loop, so a
    bad step after three blank lines reports line 4, not line 2.
    """
    src = '@set("a": "1")\n\n\nrun("b":,)'
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 4


def test_first_error_wins_when_several_lines_are_malformed():
    """Parsing aborts at the first bad line; later bad lines are never reached.

    Confirms the parser is fail-fast rather than accumulating diagnostics — the
    reported ``line_no`` is always the topmost problem.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('run("a":,)\nrun("b":,)')
    assert exc.value.line_no == 1


def test_line_no_for_a_bad_set_after_valid_steps():
    """A malformed ``@set`` below several valid steps reports its own line.

    Steps parsed before the failure are discarded (the exception propagates), so
    the only observable output is the error position.
    """
    src = 'run("a": 1)\nrun("b": 2)\n@set("c": 3)'
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string(src)
    assert exc.value.line_no == 3
    assert exc.value.detail == "invalid @set: @set value for 'c' must be a string"


def test_nexus_parse_error_is_a_value_error():
    """``NexusParseError`` subclasses ``ValueError``.

    API handlers catch ``ValueError`` broadly; if the base class changed, parse
    failures would escape as 500s instead of 400s.
    """
    with pytest.raises(ValueError) as exc:
        parse_nexus_string("nonsense line")
    assert isinstance(exc.value, NexusParseError)
    assert exc.value.line_no == 1


def test_nexus_parse_error_detail_excludes_the_line_prefix():
    """``detail`` holds only the reason; the "line N: " prefix lives in ``str()``.

    Callers that render their own line markers rely on ``detail`` being clean.
    """
    with pytest.raises(NexusParseError) as exc:
        parse_nexus_string('\n\nrun("a":,)')
    assert not exc.value.detail.startswith("line ")
    assert str(exc.value) == f"line {exc.value.line_no}: {exc.value.detail}"
