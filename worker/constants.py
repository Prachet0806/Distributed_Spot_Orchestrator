# worker/constants.py
"""
Worker-side constants.
Kept separate from orchestrator constants since worker runs independently.
"""

# Import shared constants
from common_constants import (
    WORKSPACE_ROOT,
    CHECKPOINT_DIR,
    FLAG_DIR,
    SPOT_INTERRUPT_FLAG,
    READY_FLAG,
    REQUIRED_CHECKPOINT_FILES,
)

# IMDS endpoints
IMDS_BASE_URL = "http://169.254.169.254"
IMDS_TOKEN_URL = f"{IMDS_BASE_URL}/latest/api/token"
IMDS_ACTION_URL = f"{IMDS_BASE_URL}/latest/meta-data/spot/instance-action"
IMDS_INSTANCE_ID_URL = f"{IMDS_BASE_URL}/latest/meta-data/instance-id"

# Polling intervals
SPOT_INTERRUPT_POLL_INTERVAL = 5  # seconds
IMDS_TOKEN_TTL = 21600             # 6 hours

# Job defaults
DEFAULT_MONTE_CARLO_ITERATIONS = 10_000_000
