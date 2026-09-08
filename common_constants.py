# common_constants.py
"""
Shared constants used by both orchestrator and worker.
These are deployment-specific paths that must match across all components.
"""
import os

# Workspace paths (worker node) - MUST match across orchestrator/worker/checkpoint
# Can be overridden via WORKSPACE_ROOT environment variable
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "/opt/job_workspace")
CHECKPOINT_DIR = f"{WORKSPACE_ROOT}/checkpoint"
FLAG_DIR = f"{WORKSPACE_ROOT}/flags"
SPOT_INTERRUPT_FLAG = f"{FLAG_DIR}/spot_interrupt"
READY_FLAG = f"{WORKSPACE_ROOT}/READY"

# Required checkpoint files (CRIU artifacts)
REQUIRED_CHECKPOINT_FILES = ["core-1.img", "inventory.img"]
