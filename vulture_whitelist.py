"""Vulture whitelist — intentional API surface, not dead code.

Run (all dirs at once; per-directory runs manufacture false positives
for every test double and cross-directory API):

    vulture checkpoint config infra orchestrator worker storage scripts \\
        tests vulture_whitelist.py --min-confidence 60

Categories below explain WHY each name is kept. Delete an entry only
with its code. `--min-confidence 100` alone is a useful weaker gate
(unused imports / provably-dead locals).
"""

# ------------------------------------------------------------------
# A. Required signatures (framework calls them; never referenced by name)
# ------------------------------------------------------------------
do_GET
log_message

# ------------------------------------------------------------------
# B. Signature-conformance params (mirror a production interface so
#    fakes stay drop-in; renaming would break keyword calls)
# ------------------------------------------------------------------
timeout
bucket

# ------------------------------------------------------------------
# C. Pytest fixtures (used as test parameters; vulture can't see that)
# ------------------------------------------------------------------
aws_env
aws_credentials

# ------------------------------------------------------------------
# D. Intentional forward scaffolding (owned by Tracks; wire, don't delete)
# ------------------------------------------------------------------
placement_engine  # Track C3: target selection wiring
history_store  # Track B1: HistoryStore reads
pool_store  # Track B1: PoolRegistry reads
health_server  # handle keeps the health server referenced
evaluate_recovery  # Cost&Risk recovery path (component map)
short_job_band  # ADR-007 band helper
medium_band_net_benefit_ok  # ADR-007 band helper
is_emergency_eligible  # emergency-lane readiness gate
get_checkpoint  # checkpoint lookup seam
IMDS_INSTANCE_ID_URL  # live IMDS identity path (D3 cutover)

# ------------------------------------------------------------------
# E. Future-track seams (E5 feedback, dispatcher growth, cleanup)
# ------------------------------------------------------------------
record_step
record_cleanup
get_recent
get_feedback_for_estimator
get_feedback_for_risk_model
register_default_consumer
CleanupTask
execute_cleanup_plan
get_pool
list_all  # pool registry reads (Track B1 wiring)
update_version  # pool definition versioning
MULTIPART_PART_SIZE_BYTES  # documents the ADR-016 64 MB part size; boto manages parts

# ------------------------------------------------------------------
# F. Protocol/command/event factory + lifecycle APIs (C/Q/E surface)
# ------------------------------------------------------------------
create
mark_running
mark_succeeded
mark_unknown
register

# ------------------------------------------------------------------
# G. Store / registry / ledger APIs (persistence surface; Dynamo + JSON
#    backends, tests, and future wiring call these)
# ------------------------------------------------------------------
put_plan
enrich
list_by_job
gc_candidates
unlock
put_pool
has_processed
get_finding
list_findings
resolve_finding
to_v2_report
to_v2_dag
schedule_order
decide_recovery
preempt_with_emergency
file_refusal
make_controller_fence_hooks
make_verify_handler
make_fence_hooks
supersede_and_replan
is_emergency
get_operation_status

# ------------------------------------------------------------------
# H. Reconciliation taxonomy + finding/remediation schema (Protocols #11;
#    legacy aliases kept for compat, taxonomy members for findings)
# ------------------------------------------------------------------
MANUAL
TARGET_UNREACHABLE
ORPHAN_RESOURCE
REGISTRY_INFRA_MISMATCH
MIGRATION_DANGLING
TARGET_UNOWNED
ORPHAN_CHECKPOINT
NO_ACTION
ReconnectionAction
evidence_refs
request_id
action_type
parameters
requested_at
authorized_by

# ------------------------------------------------------------------
# I. Worker/trust/telemetry surface (W1–W5, T1–T4; emulated now, live next)
# ------------------------------------------------------------------
EmulatedWorkerTransport
WorkerController
handle_command
boot_identity
parse_refusal
progress_quality
preflight_freshness
refusal_to_finding
read_progress
run_preflight

# ------------------------------------------------------------------
# J. Contract schema fields (dataclass/enum members defined by frozen
#    specs; write-only today = diagnostic payload, wire — don't delete)
# ------------------------------------------------------------------
dimension
workload_requirements_version
assessed_at
compatibility_score
recommendation_id
ranked_candidates
selected_candidate_id
generated_at
assessment_snapshots
PROVIDER_SIGNAL
RECENT_PROVISIONING
ACTIVE_PROVISIONING_RECORD
LIMITED
UNAVAILABLE
kernel_version
criu_version
reserved_instances
provisioning_timeout_seconds
affinity
anti_affinity
PREPARABLE
provisioning_success_rate
sample_count
snapshot_version
iam_readiness
network_readiness
storage_readiness
runtime_ready
duration_seconds
MIGRATION_OWNED
ORPHAN
POST_FENCING
IN_PROGRESS
DEFERRED
task_id
cleanup_type
CostComponent
CURRENT_COMPUTE
CANDIDATE_COMPUTE
CHECKPOINT_STORAGE
CHECKPOINT_TRANSFER
RESTORE
VALIDATION
EXPECTED_RISK
MIGRATION_OVERHEAD
current_cost_per_hour
checkpoint_storage_cost
checkpoint_transfer_cost
expected_risk_cost
evaluated_at
REPLAN
SKIPPED
PRECHECK
CHECKPOINT
PERSIST
PROVISION
TRANSFER
FENCE
VALIDATE
ACTIVATE
FINALIZE
CLEANUP
reason_code
replan_required
critical_path_duration_seconds
confidence_level
confidence_basis
integrity_verified
checksum
checksum_algorithm
level_reached
measurements
pre_authorized
rebalance_threshold
max_recovery_time_ratio
metadata
PoolLifecycle
ACTIVE
DEGRADED
RETIRING
RETIRED
PoolConcurrencyTracker
configure
limit_for
active_for
slot
PROVISIONING_COMPLETED
CHECKPOINT_PERSISTED
SOURCE_TERMINATED
VALIDATION_COMPLETED
INFRASTRUCTURE_MISMATCH
MIGRATION_COMPLETED
RECONCILIATION_FINDING
SPOT_PRICE_OBSERVATION
GET_CANDIDATE_READINESS
GET_OPERATION_STATUS
GET_CHECKPOINT_STATUS
GET_EXECUTION_OWNERSHIP
GET_INSTANCE_STATE
GET_MIGRATION_PLAN
command_id
issued_at
occurred_at
query_id
entity_id
actor
JOB_REGISTRY
MIGRATION_COORDINATOR
RECONCILIATION_MANAGER
CLEANUP_EXECUTOR
provisioned_at
CandidateReadinessResult
OperationStatusResult
CheckpointStatusResult
ExecutionOwnershipResult
authoritative_owner
InstanceStateResult
step_dict
bytes_transferred
tolerance_config
DEFAULT_TOLERANCE_CONTRACT
cpu_utilization
memory_utilization
expected_completion_at
estimated_at
plan_version
cleanup_status
policy_config_version
coordinator_version
state_entered_at
source_ownership
target_ownership
deadline_exceeded
abort_requested
superseded_by
received_at
processed_at
attempt_count
last_error
IDLE
EXECUTING
RECOVERING
RECONCILING
expected_execution_epoch
