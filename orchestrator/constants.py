# orchestrator/constants.py
"""
Centralized constants for the Spot Arbitrage Cluster orchestrator.
Eliminates hardcoded values and magic numbers across the codebase.
"""

# Import shared constants
from common_constants import (
    WORKSPACE_ROOT,
    CHECKPOINT_DIR,
    SPOT_INTERRUPT_FLAG,
    READY_FLAG,
    REQUIRED_CHECKPOINT_FILES,
)

# Migration timing
COOLDOWN_SECONDS = 3 * 60 * 60  # 3 hours between migrations per job
STUCK_JOB_THRESHOLD = 15 * 60    # 15 minutes - alert if job stuck in non-RUNNING state
MIGRATION_POLL_INTERVAL = 60     # 60 seconds between orchestrator polls

# Cache TTLs
PRICE_CACHE_TTL = 90             # 90 seconds (spot prices update ~5min)
CONFIG_CACHE_TTL = 300           # 5 minutes for runtime config

# AWS limits
MAX_PRICE_HISTORY = 20           # Max historical prices to track per region
DEFAULT_SPOT_PRICE_SAMPLES = 5   # Number of recent prices to fetch

# Timeouts
SSH_TIMEOUT_DEFAULT = 30         # seconds
S3_TIMEOUT_DEFAULT = 300         # 5 minutes for S3 operations
INSTANCE_PROVISION_TIMEOUT = 300 # 5 minutes for instance to be running

# Health and monitoring
HEALTH_CHECK_PORT = 8080
METRICS_ENDPOINT = "/metrics"
HEALTH_ENDPOINT = "/health"

# DynamoDB
DYNAMO_GSI_STATE_INDEX = "StateIndex"

# Job defaults
DEFAULT_WORKLOAD_TYPE = "batch"

# Rate limiting
MAX_CONCURRENT_MIGRATIONS = 3  # Max simultaneous migrations
MAX_MIGRATIONS_PER_HOUR = 20   # Global rate limit to prevent API throttling
MIGRATION_BACKOFF_SECONDS = 180 # 3 minutes between migrations globally
