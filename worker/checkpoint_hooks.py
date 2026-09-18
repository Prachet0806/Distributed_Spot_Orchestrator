# worker/checkpoint_hooks.py — quiesce/resume hooks around CRIU dump/restore.
"""The runner invokes these so application state is coherent across
freeze/restore. They only fsync the progress file — the deterministic
source of resume truth alongside the CRIU image."""
import os


def pre_checkpoint(progress_path=None):
    """Flush durable progress before the freezer runs."""
    print("Pre-checkpoint hook: flushing state")
    if progress_path and os.path.exists(progress_path):
        with open(progress_path, "a+b") as f:
            f.flush()
            os.fsync(f.fileno())
    return True


def post_restore(progress_path=None):
    """Re-establish resume invariants after restore."""
    print("Post-restore hook: resuming")
    if progress_path and not os.path.exists(progress_path):
        print("Post-restore hook: no progress file; starting from zero")
    return True
