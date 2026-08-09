"""Tests for the step registry — register / get_step / list_steps.

The registry maps step names to FlowStep classes. Built-in steps self-register
via the @register decorator when ``nexus_steps`` is imported (conftest does this).

Duplicate-registration tests must NEVER leave a new global registration behind,
or they would break every other test file that imports the suite. We therefore
monkeypatch ``STEP_REGISTRY`` with a fresh dict (or restore it manually) so the
real global registry is untouched.
"""

from __future__ import annotations

import pytest

from nexus_common.steps import registry as registry_mod
from nexus_common.steps.base import FlowStep
from nexus_common.steps.registry import (
    STEP_REGISTRY,
    get_step,
    list_steps,
    register,
)


# ── get_step: positive path ───────────────────────────────────────────────────

def test_get_step_returns_registered_builtin():
    """A built-in step ("run_command") registered on import is retrievable."""
    cls = get_step("run_command")
    assert issubclass(cls, FlowStep)
    # It is the same object stored in the global registry.
    assert cls is STEP_REGISTRY["run_command"]


def test_get_step_returns_class_with_registry_name():
    """The decorator stamps the registered name onto the class itself."""
    cls = get_step("run_command")
    assert cls._registry_name == "run_command"


# ── get_step: negative path ────────────────────────────────────────────────────

def test_get_step_unknown_raises_keyerror_with_available_list():
    """Unknown names raise KeyError listing the available (sorted) step names."""
    with pytest.raises(KeyError) as exc_info:
        get_step("definitely_not_a_real_step")

    # KeyError str() is repr-quoted; the message embeds the unknown name plus
    # the sorted available list. Check both pieces of the contract.
    message = str(exc_info.value)
    assert "definitely_not_a_real_step" in message
    assert "Available:" in message
    # The available list is the sorted registry keys.
    assert sorted(STEP_REGISTRY.keys()) == list_steps()
    for name in list_steps():
        assert name in message
    # The message embeds the *repr* of the sorted list, not some other ordering.
    assert f"Available: {sorted(STEP_REGISTRY.keys())}" in message


def test_get_step_empty_string_raises_keyerror():
    """An empty name is just another unknown key (no special-casing)."""
    with pytest.raises(KeyError) as exc_info:
        get_step("")
    assert "Available:" in str(exc_info.value)


def test_get_step_does_not_mutate_registry():
    """A failed lookup must not insert the missing key (no defaultdict-style growth)."""
    before = dict(STEP_REGISTRY)
    with pytest.raises(KeyError):
        get_step("definitely_not_a_real_step")
    assert STEP_REGISTRY == before


# ── list_steps ─────────────────────────────────────────────────────────────────

def test_list_steps_returns_sorted_names():
    """list_steps returns all registered names in sorted order."""
    names = list_steps()
    assert names == sorted(names)
    assert names == sorted(STEP_REGISTRY.keys())


def test_list_steps_includes_known_builtins():
    """Sanity: the registry is actually populated with built-in steps."""
    names = list_steps()
    assert "run_command" in names
    assert len(names) > 1


def test_list_steps_returns_independent_copy():
    """list_steps returns a fresh list — mutating it must not corrupt the registry."""
    names = list_steps()
    n_before = len(STEP_REGISTRY)
    names.append("not_a_real_step")
    names.clear()
    # The live registry and a fresh call are unaffected by mutating the result.
    assert len(STEP_REGISTRY) == n_before
    assert "run_command" in list_steps()


# ── register decorator (isolated — never mutate the global registry) ────────────

def test_register_sets_registry_name_and_stores_class(monkeypatch):
    """@register stamps _registry_name and stores the class under that name.

    We swap in a fresh dict so the global registry is never polluted.
    """
    fake_registry: dict[str, type] = {}
    monkeypatch.setattr(registry_mod, "STEP_REGISTRY", fake_registry)

    @register("temp_step_for_test")
    class TempStep(FlowStep):  # noqa: D401 - test fixture class
        """Throwaway FlowStep subclass registered only into the monkeypatched registry."""
        pass

    assert TempStep._registry_name == "temp_step_for_test"
    assert fake_registry["temp_step_for_test"] is TempStep
    # Exactly one entry was added — no extra keys.
    assert list(fake_registry) == ["temp_step_for_test"]


def test_register_returns_the_decorated_class(monkeypatch):
    """The decorator returns the *same* class object so `@register(...)` binds it."""
    monkeypatch.setattr(registry_mod, "STEP_REGISTRY", {})

    class AnotherStep(FlowStep):
        """Plain FlowStep subclass decorated manually so identity can be compared."""
        pass

    # Apply the decorator manually so we can compare identity with the input.
    decorated = register("another_temp_step")(AnotherStep)

    assert decorated is AnotherStep
    assert AnotherStep.__name__ == "AnotherStep"
    assert issubclass(AnotherStep, FlowStep)


def test_register_same_class_under_two_names(monkeypatch):
    """One class may register under several names; the stamp reflects the last."""
    fake_registry: dict[str, type] = {}
    monkeypatch.setattr(registry_mod, "STEP_REGISTRY", fake_registry)

    class Shared(FlowStep):
        """FlowStep subclass registered under two aliases to observe _registry_name."""
        pass

    register("alias_one")(Shared)
    register("alias_two")(Shared)

    assert fake_registry["alias_one"] is Shared
    assert fake_registry["alias_two"] is Shared
    # _registry_name is a single attribute, so it holds the most recent name.
    assert Shared._registry_name == "alias_two"


def test_register_rejects_duplicate_name(monkeypatch):
    """Registering two classes under one name raises ValueError naming both."""
    fake_registry: dict[str, type] = {}
    monkeypatch.setattr(registry_mod, "STEP_REGISTRY", fake_registry)

    @register("dup_step")
    class FirstStep(FlowStep):
        """First (winning) registrant for the duplicate name."""
        pass

    with pytest.raises(ValueError) as exc_info:
        @register("dup_step")
        class SecondStep(FlowStep):
            """Second registrant for the same name; its registration must be refused."""
            pass

    message = str(exc_info.value)
    assert "dup_step" in message
    assert "FirstStep" in message  # the existing registrant
    assert "SecondStep" in message  # the rejected class
    # Original registration is untouched.
    assert fake_registry["dup_step"] is FirstStep


def test_register_duplicate_rejected_even_for_same_class(monkeypatch):
    """Re-registering the *same* class under a taken name still raises (no no-op)."""
    fake_registry: dict[str, type] = {}
    monkeypatch.setattr(registry_mod, "STEP_REGISTRY", fake_registry)

    class OnlyStep(FlowStep):
        """Single class re-registered under a name it already owns."""
        pass

    register("solo")(OnlyStep)
    with pytest.raises(ValueError) as exc_info:
        register("solo")(OnlyStep)

    # The error names the class on both sides since it is the same class.
    assert "OnlyStep" in str(exc_info.value)
    assert fake_registry["solo"] is OnlyStep


def test_register_duplicate_does_not_pollute_global_registry():
    """Even a real duplicate name against the live registry is rejected, and the
    global registry is left exactly as it was (no new key, no overwrite)."""
    before = dict(STEP_REGISTRY)

    with pytest.raises(ValueError):
        @register("run_command")  # already taken by the built-in
        class ShadowStep(FlowStep):
            """Attempts to shadow the real built-in 'run_command' registration."""
            pass

    # Nothing added, nothing replaced.
    assert STEP_REGISTRY == before
    assert STEP_REGISTRY["run_command"].__name__ != "ShadowStep"
