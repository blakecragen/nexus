"""Integration tests for the step-schema introspection routes.

SUT: ``packages/server/src/nexus_server/api/routes/steps.py`` — mounted at
``/api/steps``. Two read-only handlers with no DB access and no admin gate:
``GET ""`` publishes ``to_schema()`` for every class in ``STEP_REGISTRY``, and
``GET "/{step_name}"`` publishes one. Everything they return is produced by
``nexus_common.steps.base.FlowStep.to_schema()``, so that classmethod is the real
subject and is also probed directly here.

Primary regression pinned by this file
--------------------------------------
``to_schema()`` used to publish the literal string ``"PydanticUndefined"`` as a
field's ``default``. The cause: a field declared with ``default_factory``
(e.g. ``Field(default_factory=list)``) reports ``is_required() == False`` while
its ``.default`` attribute is the ``PydanticUndefined`` sentinel rather than the
real default — and the old code stringified whatever ``.default`` held. The
sentinel therefore leaked into the public step-schema API and into the Job
Builder form, which rendered ``PydanticUndefined`` as the pre-filled value of
every list/dict parameter. The fix calls the factory instead.

Three layers guard it, deliberately overlapping:
  * ``test_no_published_field_default_is_the_pydantic_undefined_string`` — sweeps
    every registered step over HTTP. Catches any *future* step that regresses.
  * ``test_to_schema_never_leaks_the_pydantic_undefined_sentinel`` — same sweep
    at the ``to_schema()`` level, and also rejects the sentinel *object*, which
    would not survive JSON encoding and so is invisible over HTTP.
  * ``test_probe_step_default_factory_defaults_are_resolved_by_calling_the_factory``
    — a locally declared, deliberately unregistered ``FlowStep`` whose params
    cover list / dict / primitive factories. Independent of which steps happen to
    exist in ``nexus_steps``, so the regression stays pinned even if every
    current step loses its ``default_factory`` fields.

Registry notes that shape these tests
-------------------------------------
``STEP_REGISTRY`` is process-global and populated by import side effects
(``tests/conftest.py`` imports ``nexus_steps``). Registration is *not*
idempotent, so the probe step below is intentionally **not** decorated with
``@register`` — doing so at module scope would raise on a second import and take
the whole suite down. Nothing in this file mutates the registry.

Status-code contract exercised here
    * 200 — the list, and detail for every registered name.
    * 401 — missing ``Authorization`` header (``{"detail": "Not authenticated"}``)
      or an unverifiable bearer token (``"Could not validate credentials"``).
      Both are 401 on the pinned FastAPI (0.129) and are told apart by ``detail``.
    * 404 — unknown step name, with every available name in the detail string.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field
from pydantic_core import PydanticUndefined

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep
from nexus_common.steps.registry import STEP_REGISTRY, list_steps


# ── Local probe step (never registered) ───────────────────────────────────


class _ProbeParams(BaseModel):
    """Parameter model covering every ``default`` branch in ``to_schema()``.

    Each field targets one arm of the three-way default resolution:
      * ``tags`` / ``mapping`` — ``default_factory`` returning a non-primitive.
        These are the fields that used to publish ``"PydanticUndefined"``.
      * ``retries`` — ``default_factory`` returning a primitive, which must be
        published as a real JSON number rather than stringified.
      * ``timeout`` / ``label`` / ``verbose`` — plain literal defaults.
      * ``needed`` — required, so its ``.default`` is also the sentinel and the
        published default must be ``None``.
    """

    tags: list[str] = Field(default_factory=lambda: ["alpha", "beta"])
    mapping: dict[str, str] = Field(default_factory=dict)
    retries: int = Field(default_factory=lambda: 7)
    timeout: int = 30
    label: str = "probe"
    verbose: bool = False
    needed: str = Field(description="Required, no default.")


class _ProbeStep(FlowStep):
    """An unregistered ``FlowStep`` used only to exercise ``to_schema()``.

    Not decorated with ``@register`` on purpose — ``STEP_REGISTRY`` is a global
    that raises on duplicate names, and polluting it would leak this step into
    ``GET /api/steps`` for every other test in the suite. The lifecycle methods
    are stubs; nothing here is ever executed.
    """

    PARAMS_SCHEMA = _ProbeParams
    DESCRIPTION = "Probe step for schema-export tests."
    OUTPUT_KEYS = ["probe_result"]
    REQUIRES_NODE = False
    SUPPORTED_OS = ["linux"]

    def startup(self, params, ctx):
        """Unused stub — required to satisfy the ABC."""
        return {}

    def check(self, state):
        """Unused stub — required to satisfy the ABC."""
        return StepResult.SUCCESS

    def cancel(self, state):
        """Unused stub — required to satisfy the ABC."""
        return None


# ── Helpers ───────────────────────────────────────────────────────────────


def _fields_by_name(step_payload):
    """Index one step payload's ``fields`` list by parameter name.

    Args:
        step_payload: A single ``StepSchemaInfo``-shaped dict (from the API) or
            the raw ``to_schema()`` dict — the ``fields`` key is identical in
            both.

    Returns:
        dict: ``{field_name: field_dict}``.
    """
    return {f["name"]: f for f in step_payload["fields"]}


def _step_by_name(payload, name):
    """Pull one step out of the ``GET /api/steps`` list payload.

    Args:
        payload: The decoded list response.
        name: Registry key to find.

    Returns:
        dict: The matching step entry.

    Raises:
        AssertionError: If the step is absent, with the available names — a
            clearer failure than a bare ``StopIteration``.
    """
    matches = [s for s in payload if s["name"] == name]
    assert matches, f"step {name!r} missing from response; got {[s['name'] for s in payload]}"
    return matches[0]


# ── GET /api/steps — catalog shape ────────────────────────────────────────


def test_get_all_steps_returns_one_entry_per_registered_step(auth_client):
    """The catalog contains exactly the registry's keys — no more, no fewer.

    The handler iterates ``list_steps()`` and indexes ``STEP_REGISTRY``, so this
    is the assertion that a step which fails to import (bad dependency, syntax
    error in a plugin module) silently disappears from the palette rather than
    erroring. Comparing against ``list_steps()`` rather than a hard-coded count
    keeps the test correct as steps are added.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["name"] for s in body] == list_steps()
    assert len(body) == len(STEP_REGISTRY)


def test_get_all_steps_is_sorted_by_step_name(auth_client):
    """The catalog is alphabetically ordered because ``list_steps()`` sorts.

    The UI renders the step palette in response order and does not sort, so an
    unsorted (import-order) response would make the palette shuffle between
    server restarts.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    names = [s["name"] for s in resp.json()]
    assert names == sorted(names)
    assert len(names) == len(set(names)), "registry keys must be unique"


def test_get_all_steps_publishes_exactly_the_declared_schema_keys(auth_client):
    """Every entry carries the ``StepSchemaInfo`` key set and nothing else.

    In particular ``large_output`` must be absent: ``to_schema()`` emits it but
    ``StepSchemaInfo`` does not declare it, so ``StepSchemaInfo(**schema)`` drops
    it. Pinning the key set documents that the hint never reaches the frontend
    today, and makes adding it to the model a visible, deliberate change.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    for step in resp.json():
        assert set(step) == {
            "name", "description", "requires_node", "supported_os",
            "output_keys", "fields", "rules", "os_variants",
        }, step["name"]
        assert "large_output" not in step


def test_get_all_steps_field_entries_have_the_declared_field_schema_keys(auth_client):
    """Each published field is a complete ``FieldSchema`` — the form's contract.

    ``JobBuilder.tsx`` reads all six keys to choose a widget, label it, prefill
    it and mark it required. A missing key would render a broken input rather
    than fail loudly, so the shape is asserted here instead.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    for step in resp.json():
        for field in step["fields"]:
            assert set(field) == {
                "name", "required", "description", "default", "examples",
                "field_type",
            }, (step["name"], field.get("name"))
            assert isinstance(field["name"], str) and field["name"]
            assert isinstance(field["required"], bool)
            assert isinstance(field["examples"], list)
            assert all(isinstance(e, str) for e in field["examples"])


def test_every_step_publishes_a_non_empty_description(auth_client):
    """``DESCRIPTION`` is user-facing palette text, so no step may ship without one.

    ``FlowStep.DESCRIPTION`` defaults to ``""``; a step author who forgets it gets
    a blank entry in the step picker with no other symptom. This is the only
    place that omission is caught.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    missing = [s["name"] for s in resp.json() if not s["description"].strip()]
    assert missing == [], f"steps with no DESCRIPTION: {missing}"


# ── THE REGRESSION: default_factory defaults ──────────────────────────────


def test_no_published_field_default_is_the_pydantic_undefined_string(auth_client):
    """No step publishes the literal string ``"PydanticUndefined"`` as a default.

    This is the regression the ``to_schema()`` fix addressed. A field declared
    with ``default_factory`` reports ``is_required() == False`` but its
    ``.default`` is the ``PydanticUndefined`` sentinel, so the old
    ``str(field_info.default)`` published ``"PydanticUndefined"`` as the field's
    default — visible in ``GET /api/steps`` and prefilled into the Job Builder
    form for every list/dict parameter. Sweeping every registered step means a
    newly added step with a ``default_factory`` cannot reintroduce it.
    """
    resp = auth_client.get("/api/steps")

    assert resp.status_code == 200, resp.text
    offenders = [
        (s["name"], f["name"], f["default"])
        for s in resp.json()
        for f in s["fields"]
        if isinstance(f["default"], str) and "PydanticUndefined" in f["default"]
    ]
    assert offenders == [], f"sentinel leaked into published defaults: {offenders}"


def test_to_schema_never_leaks_the_pydantic_undefined_sentinel():
    """``to_schema()`` itself yields no sentinel — object *or* stringified form.

    The HTTP sweep above cannot see the raw ``PydanticUndefined`` object (JSON
    encoding would either stringify it or fail), so the same sweep is repeated
    against ``to_schema()`` directly. The belt-and-braces ``is PydanticUndefined``
    check in the source is exactly what this asserts, and every published default
    is additionally required to be a JSON-safe value: a scalar, ``None``, or a
    (possibly nested) list/dict of the same.
    """
    for name in list_steps():
        schema = STEP_REGISTRY[name].to_schema()
        for field in schema["fields"]:
            default = field["default"]
            assert default is not PydanticUndefined, (name, field["name"])
            assert not (isinstance(default, str) and "PydanticUndefined" in default), (
                name, field["name"], default,
            )
            assert default is None or isinstance(default, (str, int, float, bool, list, dict)), (
                name, field["name"], type(default),
            )


def test_probe_step_default_factory_defaults_are_resolved_by_calling_the_factory():
    """A ``default_factory`` field publishes the factory's *value* as real JSON, not the sentinel.

    The narrowest possible pin on the fix, using a locally declared step so it
    holds regardless of what ``nexus_steps`` contains. Covers all three factory
    outcomes: a non-empty list and a dict are published as their actual JSON
    value (not a stringified repr — see the module docstring on
    ``to_schema()``'s list/dict serialization fix), while a factory returning a
    primitive keeps its native type.
    """
    schema = _ProbeStep.to_schema()
    fields = _fields_by_name(schema)

    assert fields["tags"]["default"] == ["alpha", "beta"]
    assert fields["mapping"]["default"] == {}
    assert fields["retries"]["default"] == 7
    assert isinstance(fields["retries"]["default"], int)
    for name in ("tags", "mapping", "retries"):
        assert fields[name]["required"] is False
        assert "PydanticUndefined" not in str(fields[name]["default"])


def test_probe_step_required_field_publishes_a_null_default():
    """A required field's default is ``None``, never the sentinel it really holds.

    A required field's ``FieldInfo.default`` is also ``PydanticUndefined``. There
    is nothing meaningful to publish, so the branch order in ``to_schema()``
    (``default_factory`` first, then ``is_required()``) must land it on ``None``
    — otherwise every required parameter would prefill the form with the sentinel
    string too.
    """
    fields = _fields_by_name(_ProbeStep.to_schema())

    assert fields["needed"]["required"] is True
    assert fields["needed"]["default"] is None
    assert fields["needed"]["description"] == "Required, no default."


def test_probe_step_literal_defaults_keep_their_native_json_type():
    """Plain literal defaults pass through unstringified with their real type.

    The stringify step only applies to non-primitives. An over-eager ``str()``
    would turn ``timeout=30`` into ``"30"`` and ``verbose=False`` into
    ``"False"`` — the latter being truthy in JavaScript, which would silently
    flip a checkbox default in the Job Builder.
    """
    fields = _fields_by_name(_ProbeStep.to_schema())

    assert fields["timeout"]["default"] == 30 and isinstance(fields["timeout"]["default"], int)
    assert fields["label"]["default"] == "probe"
    assert fields["verbose"]["default"] is False


def test_registered_steps_with_list_factories_publish_their_real_defaults(auth_client):
    """Real ``default_factory`` fields in ``nexus_steps`` publish parseable values.

    Complements the probe step by asserting the shipped steps that actually use
    ``default_factory`` — the ones that used to display ``PydanticUndefined`` in
    the UI. Each published default must be the ACTUAL list/dict as real JSON,
    not a stringified repr: JobBuilder's buildDefaultParams() pre-fills the
    step's form with this value and posts it straight back on submit, and a
    stringified default (e.g. ``"['cpu', 'memory']"``) used to be submitted
    back as a string and rejected by Pydantic with "Input should be a valid
    list" — meaning a step couldn't even be submitted with its own defaults.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected = {
        ("health_check", "checks"): ["cpu", "memory", "disk", "network"],
        ("run_python", "args"): [],
        ("run_python", "env"): {},
        ("run_script", "args"): [],
        ("gem5_run_simulation", "script_args"): [],
        ("docker_ensure_container", "mounts"): [],
    }
    for (step_name, field_name), want in expected.items():
        field = _fields_by_name(_step_by_name(body, step_name))[field_name]
        assert field["required"] is False, (step_name, field_name)
        assert field["default"] == want, (step_name, field_name, field["default"])
        assert type(field["default"]) is type(want), (step_name, field_name)


def test_registered_steps_scalar_defaults_keep_their_native_json_type(auth_client):
    """Shipped scalar defaults arrive as JSON numbers / booleans / strings.

    The Job Builder feeds these straight back as step params, so a stringified
    ``"3600"`` would fail the server's Pydantic validation on resubmit, and a
    stringified ``"False"`` would be coerced to ``True``. One of each primitive
    type is checked.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    cases = [
        ("run_command", "timeout", 3600),
        ("gem5_run_simulation", "timeout", 7200),
        ("gem5_run_simulation", "collect_stats", True),
        ("docker_ensure_container", "recreate", False),
        ("git_pull", "remote", "origin"),
        ("jump", "on", "always"),
    ]
    for step_name, field_name, want in cases:
        field = _fields_by_name(_step_by_name(body, step_name))[field_name]
        assert field["default"] == want, (step_name, field_name, field["default"])
        assert type(field["default"]) is type(want), (step_name, field_name)


def test_optional_fields_without_a_declared_default_publish_null(auth_client):
    """An ``X | None = None`` parameter publishes ``default: null``, not ``"None"``.

    The most common optional shape in the step library. ``None`` short-circuits
    the stringify branch (`default_val is not None and ...`), so it survives as a
    JSON null — which is what lets the form leave the input blank instead of
    prefilling the four characters ``None``.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for step_name, field_name in [
        ("run_command", "working_dir"),
        ("git_clone", "branch"),
        ("gem5_collect_results", "m5out_path"),
    ]:
        field = _fields_by_name(_step_by_name(body, step_name))[field_name]
        assert field["required"] is False, (step_name, field_name)
        assert field["default"] is None, (step_name, field_name, field["default"])


# ── Published metadata: types, rules, examples, targeting ─────────────────


def test_field_type_hints_map_annotations_to_widget_names(auth_client):
    """``field_type`` is the coarse widget selector, matched by annotation text.

    ``_simplify_type`` matches ordered substrings of ``str(annotation)``, so the
    interesting cases are the ones where a naive mapping would be wrong: an
    ``Optional[list[str]]`` must still be "list" (list is tested first) and a
    ``bool`` must not be captured by the earlier ``int`` check. A wrong guess
    only degrades UX — Pydantic remains authoritative — which is precisely why it
    would otherwise go unnoticed.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    cases = [
        ("run_command", "command", "string"),
        ("run_command", "timeout", "integer"),
        ("sleep", "seconds", "number"),
        ("docker_ensure_container", "recreate", "boolean"),
        ("run_script", "args", "list"),
        ("run_python", "env", "object"),
    ]
    for step_name, field_name, want in cases:
        field = _fields_by_name(_step_by_name(body, step_name))[field_name]
        assert field["field_type"] == want, (step_name, field_name, field["field_type"])


def test_examples_are_published_as_strings_even_for_list_valued_examples(auth_client):
    """Non-string examples are stringified so the form renderer needs no type switch.

    ``FieldSchema.examples`` is ``list[str]``. A step author may legitimately
    write ``examples=[["cpu", "memory"]]`` for a list parameter, and ``to_schema``
    coerces each element — without that the response model would reject the step
    and take the whole catalog down with a 500.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    checks = _fields_by_name(_step_by_name(body, "health_check"))["checks"]
    assert checks["examples"] == ["['cpu', 'memory']", "['disk']"]

    mounts = _fields_by_name(_step_by_name(body, "docker_ensure_container"))["mounts"]
    assert mounts["examples"] == ["['/Users/me/Desktop/gem5']"]

    command = _fields_by_name(_step_by_name(body, "run_command"))["command"]
    assert command["examples"] == ["echo hello", "ls -la /tmp"]


def test_required_and_optional_rules_are_published_for_derived_rule_sets(auth_client):
    """The default ``input_rules()`` publishes one rule per parameter.

    The frontend mirrors these to pre-validate the form. ``run_command`` uses the
    derived rule set, so ``command`` (no default) must appear as ``required`` and
    every defaulted field as ``optional`` — that split is how the form decides
    which inputs to mark with an asterisk.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text

    rules = _step_by_name(resp.json(), "run_command")["rules"]
    by_type = {}
    for rule in rules:
        by_type.setdefault(rule["rule_type"], []).extend(rule["fields"])

    assert by_type["required"] == ["command"]
    assert sorted(by_type["optional"]) == ["shell", "timeout", "working_dir"]


def test_at_least_one_rule_is_published_for_run_python(auth_client):
    """``run_python`` publishes a single multi-field ``at_least_one`` rule.

    It overrides ``input_rules()`` wholesale, which — as the base class warns —
    replaces rather than extends the derived list. So the published rules are
    just the one ``at_least_one`` over ``code``/``script_path``, and its
    ``fields`` list has more than one element (the only rule type where that is
    true). The UI keys off that to render an either/or hint.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text

    rules = _step_by_name(resp.json(), "run_python")["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "at_least_one"
    assert sorted(rules[0]["fields"]) == ["code", "script_path"]


def test_context_satisfiable_rule_loses_its_context_key_over_http(auth_client):
    """``InputRuleSchema`` drops the ``context_key`` that ``to_schema()`` emits.

    ``ContextSatisfiableRule.to_schema()`` includes ``context_key`` (the upstream
    output that satisfies the field), but ``InputRuleSchema`` does not declare it,
    so it is silently discarded when the route builds the response model. The
    frontend therefore cannot tell the user *which* upstream key would satisfy
    ``m5out_path`` — only the human-readable ``description`` survives. Documented
    here so adding the field is a deliberate act, not a surprise.
    """
    raw = STEP_REGISTRY["gem5_collect_results"].to_schema()
    assert raw["rules"][0]["context_key"] == "m5out_path"

    resp = auth_client.get("/api/steps/gem5_collect_results")
    assert resp.status_code == 200, resp.text
    rule = resp.json()["rules"][0]

    assert rule["rule_type"] == "context_satisfiable"
    assert rule["fields"] == ["m5out_path"]
    assert set(rule) == {"rule_type", "fields", "description"}
    assert "context_key" not in rule
    assert "gem5_run_simulation" in rule["description"]


def test_control_plane_steps_report_requires_node_false(auth_client):
    """``sleep`` and ``jump`` are the control-plane steps — no agent needed.

    ``requires_node=False`` is what routes a step to
    ``JobRunner._execute_local_step`` instead of the scheduler. Getting it wrong
    for these two would make a job with no eligible node hang waiting for an
    agent to run ``sleep`` on.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert _step_by_name(body, "sleep")["requires_node"] is False
    assert _step_by_name(body, "jump")["requires_node"] is False
    assert _step_by_name(body, "run_command")["requires_node"] is True
    assert _step_by_name(body, "gem5_run_simulation")["requires_node"] is True


def test_supported_os_and_os_variants_are_published_per_step(auth_client):
    """``supported_os`` narrows scheduling; ``os_variants`` carries per-OS defaults.

    The scheduler matches a node's ``os_type`` against ``supported_os``, so
    publishing it lets the UI grey out steps a pool cannot run. ``os_variants``
    defaults to ``{}`` for steps with no per-OS defaults — the response must be
    an empty object, not null, because the frontend indexes into it directly.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    gem5 = _step_by_name(body, "gem5_run_simulation")
    assert sorted(gem5["supported_os"]) == ["linux", "macos"]
    assert set(gem5["os_variants"]) == {"linux", "macos"}

    assert set(_step_by_name(body, "run_command")["os_variants"]) == {
        "linux", "macos", "windows",
    }
    assert _step_by_name(body, "git_clone")["os_variants"] == {}

    for step in body:
        assert isinstance(step["os_variants"], dict), step["name"]
        assert step["supported_os"], step["name"]


def test_output_keys_are_published_for_chainable_steps(auth_client):
    """``output_keys`` is the contract downstream steps validate against.

    Submit-time validation pre-seeds these keys into the validation context so a
    ``ContextSatisfiableRule`` downstream passes. ``gem5_run_simulation`` must
    therefore publish ``m5out_path`` — the exact key
    ``gem5_collect_results.m5out_path`` is satisfied by — or chained gem5 jobs
    would be rejected at submit. Control-plane steps contribute nothing.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    gem5_keys = _step_by_name(body, "gem5_run_simulation")["output_keys"]
    assert "m5out_path" in gem5_keys
    collect_rule = _step_by_name(body, "gem5_collect_results")["rules"][0]
    assert collect_rule["fields"] == ["m5out_path"]

    assert _step_by_name(body, "sleep")["output_keys"] == []
    assert _step_by_name(body, "jump")["output_keys"] == []


def test_published_name_is_the_registry_key_not_the_python_class_name(auth_client):
    """Every entry's ``name`` is the ``@register`` string, not ``cls.__name__``.

    ``StepConfig.step`` is matched against this value, so publishing
    ``RunCommandStep`` instead of ``run_command`` would make every job built from
    the palette fail submission with "unknown step". The registry stamps
    ``_registry_name`` for exactly this reason.
    """
    resp = auth_client.get("/api/steps")
    assert resp.status_code == 200, resp.text

    for step in resp.json():
        cls = STEP_REGISTRY[step["name"]]
        assert step["name"] == cls._registry_name
        assert step["name"] != cls.__name__


def test_unregistered_step_class_falls_back_to_its_python_class_name():
    """``to_schema()`` on a class that was never registered reports ``cls.__name__``.

    The ``getattr(cls, "_registry_name", cls.__name__)`` fallback only fires
    off the API path — mainly in tests and for a step class under development.
    Pinned so the fallback stays a *fallback*: if it ever became the primary
    source, every published name would silently change to the class name.
    """
    schema = _ProbeStep.to_schema()

    assert schema["name"] == "_ProbeStep"
    assert "_ProbeStep" not in STEP_REGISTRY
    assert schema["requires_node"] is False
    assert schema["output_keys"] == ["probe_result"]
    assert schema["large_output"] is False


# ── GET /api/steps/{step_name} — detail + 404 ─────────────────────────────


def test_get_step_detail_returns_the_same_payload_as_the_catalog_entry(auth_client):
    """Detail and list agree byte-for-byte for every registered step.

    Both handlers call the same ``to_schema()`` and build the same response
    model, so any divergence means one of them started post-processing the
    payload. The UI fetches the list for the palette and the detail when a step
    is selected; a mismatch would make the form change shape on click.
    """
    listed = auth_client.get("/api/steps")
    assert listed.status_code == 200, listed.text

    for step in listed.json():
        detail = auth_client.get(f"/api/steps/{step['name']}")
        assert detail.status_code == 200, detail.text
        assert detail.json() == step


def test_get_step_detail_unknown_step_returns_404_listing_available_steps(auth_client):
    """An unregistered name is a 404 whose detail enumerates every valid name.

    Intentional self-diagnosis for typos in a job definition: the message names
    the bad step and every alternative. Also the primary symptom of the
    server/agent registry skew described in ``registry.py`` — a step present on
    one side and not the other.
    """
    resp = auth_client.get("/api/steps/definitely_not_a_step")

    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("Step 'definitely_not_a_step' not found. Available: ")
    for name in list_steps():
        assert name in detail


@pytest.mark.parametrize("variant", ["Run_Command", "RUN_COMMAND", "run command", "run_command "])
def test_get_step_detail_lookup_is_exact_and_case_sensitive(auth_client, variant):
    """Near-miss spellings 404 rather than resolving to the real step.

    The registry is a plain dict keyed by the exact ``@register`` string, and that
    string is part of the API contract (saved templates and ``.nexus`` files
    embed it). Accepting case-insensitive or whitespace-padded variants here
    would diverge from what ``jobs.submit_job`` accepts, so the UI could offer a
    name the submit endpoint then rejects.
    """
    resp = auth_client.get("/api/steps/" + variant.replace(" ", "%20"))

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


def test_get_step_detail_for_every_registered_name_succeeds(auth_client):
    """Every registry key is individually fetchable — no name breaks path routing.

    Step names travel in the URL path, so a key containing a ``/`` or another
    reserved character would 404 despite being registered. Iterating the whole
    registry is the only way to catch that as steps are added.
    """
    for name in list_steps():
        resp = auth_client.get(f"/api/steps/{name}")
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json()["name"] == name


# ── Auth ──────────────────────────────────────────────────────────────────


def test_get_all_steps_without_authorization_is_rejected(client):
    """The catalog is not public — 401 ``"Not authenticated"`` with no header.

    Step schemas are "public to authenticated users" metadata, not anonymous
    metadata: the descriptions and examples leak internal paths (``/tmp/nexus_*``,
    gem5 config layouts). Rejected by ``HTTPBearer`` before the handler runs, so
    no registry read happens.
    """
    resp = client.get("/api/steps")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"


def test_get_step_detail_without_authorization_is_rejected(client):
    """The detail endpoint is gated identically to the catalog.

    Checked separately because the two handlers declare their dependency
    independently — it would be easy to add a route here and forget the
    ``CurrentUser`` parameter, since the value is unused in the body.
    """
    resp = client.get("/api/steps/run_command")

    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Not authenticated"


def test_step_routes_reject_a_malformed_bearer_token(client):
    """An unverifiable token gets the generic 401 from ``get_current_user``.

    Distinguished from the missing-header case by ``detail`` only. Asserting both
    strings keeps the two failure modes separable for clients even though FastAPI
    0.129 answers 401 for each.
    """
    headers = {"Authorization": "Bearer not.a.jwt"}

    listed = client.get("/api/steps", headers=headers)
    detail = client.get("/api/steps/run_command", headers=headers)

    assert listed.status_code == 401, listed.text
    assert listed.json()["detail"] == "Could not validate credentials"
    assert detail.status_code == 401, detail.text
    assert detail.json()["detail"] == "Could not validate credentials"


def test_unknown_step_404_still_requires_authentication(client):
    """Auth runs before the registry lookup, so an anonymous typo is 401 not 404.

    Ordering matters: a 404 that fired first would let an unauthenticated caller
    enumerate the whole step registry from the error detail, which is exactly what
    that message contains.
    """
    resp = client.get("/api/steps/definitely_not_a_step")

    assert resp.status_code == 401, resp.text
    assert "Available" not in resp.text


def test_step_routes_are_not_admin_gated(auth_client, admin_client):
    """A regular user sees the same catalog as an admin — no role check exists.

    Documents the deliberate absence of an admin gate noted in the module
    docstring: the palette must be readable by anyone who can build a job. If a
    ``require_admin`` were ever added, this test fails and forces the frontend's
    non-admin experience to be reconsidered.

    Both client fixtures mutate the same underlying ``TestClient``, so the
    regular-user request is issued *before* ``admin_client`` overwrites the
    header — the ordering here is load-bearing.
    """
    as_user = auth_client.get("/api/steps")
    assert as_user.status_code == 200, as_user.text

    as_admin = admin_client.get("/api/steps")
    assert as_admin.status_code == 200, as_admin.text
    assert as_admin.json() == as_user.json()
