"""V1 LEGACY transition table — FROZEN.

V2 uses the four-domain model (Job/Migration/PlanStep/Operation) with
`storage/v2_transitions.py` + `Registry.transition(CAS)`. This table is kept
only for `--engine v1` / legacy registry compat (ADR-024). Do not add states.
"""
from enum import Enum


class JobState(str, Enum):
    RUNNING = "RUNNING"
    CHECKPOINTING = "CHECKPOINTING"
    UPLOADING = "UPLOADING"
    PROVISIONING = "PROVISIONING"
    VALIDATING = "VALIDATING"
    DOWNLOADING = "DOWNLOADING"
    RESTORING = "RESTORING"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


ALLOWED_TRANSITIONS = {
    JobState.RUNNING: {JobState.CHECKPOINTING},
    JobState.CHECKPOINTING: {JobState.UPLOADING, JobState.FAILED},
    JobState.UPLOADING: {JobState.PROVISIONING, JobState.FAILED},
    JobState.PROVISIONING: {JobState.VALIDATING, JobState.FAILED},
    JobState.VALIDATING: {JobState.DOWNLOADING, JobState.FAILED},
    JobState.DOWNLOADING: {JobState.RESTORING, JobState.FAILED},
    JobState.RESTORING: {JobState.RUNNING, JobState.FAILED},
    JobState.FAILED: {JobState.CHECKPOINTING, JobState.TERMINATED},
    JobState.TERMINATED: set(),
}


def normalize_state(value):
    if isinstance(value, JobState):
        return value
    if value is None:
        return None
    return JobState(str(value))


def is_transition_allowed(current_state, next_state):
    current = normalize_state(current_state)
    nxt = normalize_state(next_state)
    if current is None or nxt is None:
        return False
    if current == nxt:
        return True
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    return nxt in allowed
