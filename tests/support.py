"""Explicit V2 test collaborators; production defaults remain fail-closed."""
from orchestrator.validator import Validator


def emulated_validator(**kwargs):
    kwargs.setdefault("infra_probe", lambda **_: {"passed": True, "probe": "emulated"})
    kwargs.setdefault("app_probe", lambda **_: {"passed": True, "probe": "emulated"})
    return Validator(**kwargs)
