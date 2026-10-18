# orchestrator/main.py
import argparse
import logging
import logging.config
import os
import re
import time
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import yaml

from orchestrator.watcher import SpotPriceWatcher
from orchestrator.workload_estimator import WorkloadEstimator, WorkloadObservation, EstimatorConfig
from orchestrator.workload_requirements import WorkloadRequirements, GPURequirement, PlacementConstraints
from orchestrator.candidate_pool import CandidatePool, CandidatePoolRegistry, PoolRuntimeProfile, PoolCapacityProfile
from orchestrator.candidate_compatibility import CompatibilityEngine
from orchestrator.candidate_readiness import ReadinessEngine, IAMReadiness, NetworkReadiness, StorageReadiness, CapacityEvidence, CapacityStatus, EvidenceSource, ReadinessStatus
from orchestrator.candidate_placement import PlacementEngine, PlacementPolicy, PlacementInput
from orchestrator.recovery_feasibility import RecoveryFeasibilityEngine, build_emergency_plan_steps
from orchestrator.cost_risk_evaluator import CostRiskEvaluator
from orchestrator.policy_engine import PolicyEngine, MigrationRegime, ArbitragePolicyConfig
from orchestrator.migration_planner import MigrationPlanner
from orchestrator.migration_coordinator import MigrationCoordinator
from orchestrator.validator import Validator
from storage.plan_store import PlanStore
from storage.checkpoint_store import CheckpointStore
from storage.audit_store import AuditStore
from storage.history_store import HistoryStore
from storage.pool_registry import PoolRegistryStore
from orchestrator.reconciliation_manager import ReconciliationManager, ReconciliationTrigger
from orchestrator.cleanup_executor import CleanupExecutor
from orchestrator.migration_history import MigrationHistory
from orchestrator.provisioner import Provisioner
from orchestrator.checkpoint_manager import CheckpointManager
from orchestrator.transfer_manager import TransferManager

from orchestrator.config_loader import load_runtime_config
from orchestrator.metrics import get_metrics
from orchestrator.constants import (
    COOLDOWN_SECONDS,
    STUCK_JOB_THRESHOLD,
    MIGRATION_POLL_INTERVAL,
    PRICE_CACHE_TTL,
    HEALTH_CHECK_PORT,
    MAX_CONCURRENT_MIGRATIONS,
    MAX_MIGRATIONS_PER_HOUR,
    MIGRATION_BACKOFF_SECONDS,
)
from orchestrator.rate_limiter import TokenBucketRateLimiter
from storage.dynamo_registry import DynamoRegistry
from storage.backend import encode, decode


def load_logging_config(path="config/logging.yaml"):
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        logging.config.dictConfig(cfg)
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}',
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth_token = os.getenv("HEALTH_AUTH_TOKEN")
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)

        if auth_token and query_params.get("token", [None])[0] != auth_token:
            self.send_response(403)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"forbidden"}')
            return

        if parsed.path == "/metrics":
            payload = get_metrics().render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format, *args):
        return


def start_health_server(port=8080, timeout=10):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.timeout = timeout
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _stage_pending(registry, job_id, attrs, log=None):
    """Attr-only PENDING staging (same-state CAS). Never raises: a
    conflict means a concurrent writer won — the next tick re-reads."""
    try:
        return registry.transition(job_id, "PENDING", **attrs)
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Job %s pending staging skipped: %s", job_id, exc)
            except Exception:
                pass


def _handle_pending_job(job, job_id, current_region, *, prices,
                        instance_type, pool_store, compatibility_engine,
                        placement_engine, provisioner, rate_limiter,
                        registry, transport_for, pending_attempt,
                        tele_attempt, now, log=None, launch_handler=None):
    """Own one PENDING tick: provision-then-promote. Never raises, never
    migrates. All worker facts (IP/instance/pid) are provisioner- or
    telemetry-observed, never user-supplied (invariants 1-3)."""
    try:
        # 1. Provisioned (has IP): throttled telemetry promotion.
        if job.get("public_ip"):
            last = tele_attempt.get(job_id, 0.0)
            if now - last < PENDING_TELEMETRY_POLL_SECONDS:
                return
            if launch_handler is not None and not job.get("pid"):
                if not job.get("launch_operation_id"):
                    from orchestrator.models_v2 import new_ulid_like
                    staged = _stage_pending(registry, job_id, {"launch_operation_id": new_ulid_like()}, log)
                    if staged is None:
                        return
                    job = staged
                launch_result = launch_handler(job)
                staged = _stage_pending(registry, job_id, {"launch_operation_id": launch_result["operation_id"], "pid": launch_result["pid"], "process_start": launch_result.get("process_start", "")}, log)
                if staged is None:
                    return
                job = staged
            tele_attempt[job_id] = now
            try:
                if _try_promote_pending_job(
                        job, job_id, registry, transport_for, log):
                    tele_attempt.pop(job_id, None)
            except Exception:
                pass
            return
        # 2. Provisioned before but IP missing: failed/partial record.
        # Wait for operator/reconciliation; never hot-loop paid calls.
        if job.get("provisioned_instance_id"):
            if log is not None:
                try:
                    log.debug("Job %s provisioned but unreachable; "
                              "awaiting reconciliation", job_id)
                except Exception:
                    pass
            return
        # 3. Unprovisioned: retry-throttled initial placement + provision.
        last = pending_attempt.get(job_id, 0.0)
        if now - last < PENDING_PROVISION_RETRY_SECONDS:
            return
        pending_attempt[job_id] = now
        requirements = _requirements_for_job(job, log)
        candidate_pools = {}
        for r in prices:
            pool_id = f"pool-{r}-{instance_type}"
            try:
                _def = pool_store.get_pool(pool_id).get("definition", {})
            except Exception:
                _def = None
            _def = _def or {}
            digest = _def.get("runtime_artifact_digest")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                continue
            candidate_pools[r] = CandidatePool(
                pool_id=pool_id,
                provider=_def.get("provider", "aws"),
                account_id=_def.get("account_id", ""),
                region=_def.get("region", r),
                availability_zone=_def.get("availability_zone", f"{r}a"),
                instance_type=_def.get("instance_type", instance_type),
                architecture=_def.get("architecture", "x86_64"),
                runtime_profile=PoolRuntimeProfile(
                    artifact_digest=digest,
                    architecture=_def.get("architecture", "x86_64"),
                ),
                capacity_profile=PoolCapacityProfile(max_instances=_def.get("max_instances", 1)),
            )
            try:
                candidate_pools[r].lifecycle = _def.get("lifecycle", "ACTIVE")
            except Exception:
                pass
        target = _select_initial_pool(
            job, requirements, prices, candidate_pools,
            compatibility_engine, placement_engine, log)
        if target is None:
            _stage_pending(registry, job_id,
                           {"admission_status": "NO_ELIGIBLE_POOL"}, log)
            return
        if not rate_limiter.acquire(timeout=0):
            if log is not None:
                try:
                    log.info("Job %s admission rate-limited; retry later",
                             job_id)
                except Exception:
                    pass
            return
        try:
            try:
                _def = pool_store.get_pool(
                    f"pool-{target}-{instance_type}").get("definition", {})
            except Exception:
                _def = None
            tags = {"job_id": job_id, "admission": "initial",
                    "execution_epoch": str(job.get("execution_epoch", 0))}
            if not _def:
                raise ValueError("No authoritative initial pool definition")
            op_id = job.get("admission_operation_id")
            if not op_id:
                from orchestrator.models_v2 import new_ulid_like
                op_id = new_ulid_like()
                if _stage_pending(registry, job_id, {"admission_operation_id": op_id}, log) is None:
                    return
            result = provisioner.provision_from_pool(_def, tags=tags, operation_id=op_id)
            _stage_pending(registry, job_id, {
                "public_ip": getattr(result, "public_ip", None),
                "instance_id": getattr(result, "instance_id", None),
                "provisioned_instance_id": getattr(
                    result, "instance_id", None),
                "pool_id": f"pool-{target}-{instance_type}",
                "admission_status": "provisioned",
            }, log)
            if log is not None:
                try:
                    log.info("Job %s admitted worker %s in %s "
                             "(awaiting heartbeat)", job_id,
                             getattr(result, "instance_id", "?"), target)
                except Exception:
                    pass
        except Exception as exc:
            _stage_pending(registry, job_id, {
                "admission_status": "provision-failed",
                "admission_error": str(exc)[-500:],
            }, log)
            if log is not None:
                try:
                    log.warning("Job %s admission provisioning failed: %s",
                                job_id, exc)
                except Exception:
                    pass
        finally:
            try:
                rate_limiter.release()
            except Exception:
                pass
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Job %s pending handling skipped: %s", job_id, exc)
            except Exception:
                pass


def _spot_flag_detected(host, flag_path, log):
    try:
        from orchestrator.utils import SSHClient
        ssh = SSHClient(host)
        ssh.connect()
        result = ssh.run_command(
            f"test -f {flag_path} && echo 1 || echo 0",
            check=False,
        )
        return result.stdout.strip() == "1"
    except Exception as exc:
        log.warning("Spot flag check failed for %s: %s", host, exc)
        return False
    finally:
        try:
            ssh.close()
        except Exception:
            pass


def query_checkpoint_durable(checkpoint_store, job_id, log=None) -> bool:
    """Authoritative checkpoint-durability check (invariant 5).

    Returns True only when the store holds a DURABLE/VALIDATED checkpoint
    for this job's lineage. Missing/uncertain/unreadable is False — never
    inferred True from migration intent.
    """
    try:
        return checkpoint_store.latest_durable(job_id) is not None
    except Exception as exc:
        if log is not None:
            try:
                log.warning("Checkpoint durability lookup failed for %s: %s",
                            job_id, exc)
            except Exception:
                pass
        return False


def _requirements_for_job(job, log=None) -> "WorkloadRequirements":
    """Derive placement requirements from the job's workload contract.

    PA-10: replaces the fixed 1-CPU/1GB fabrication. Jobs admitted via
    `spotctl run` carry `workload_contract`; records without one are rejected.
    """
    contract = (job or {}).get("workload_contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("resources"), dict):
        raise ValueError("V2 jobs require a workload contract with resources")
    res = contract["resources"]
    try:
        cpu = int(res["cpu"])
        if cpu <= 0:
            raise ValueError("CPU requirement must be positive")
    except (TypeError, ValueError):
        raise ValueError("invalid CPU requirement")
    try:
        mem = int(res["memory_mb"])
        if mem <= 0:
            raise ValueError("memory requirement must be positive")
    except (TypeError, ValueError):
        raise ValueError("invalid memory requirement")
    gpu = bool(res.get("gpu", False))
    try:
        storage = max(1, int(res.get("storage_gb", 10)))
    except (TypeError, ValueError):
        storage = 10
    # Phase B drift close-out: propagate the contract's runtime identity
    # and placement constraints so digest mismatch fails deterministically
    # at compatibility time (never a migration-time surprise).
    rt = contract.get("runtime", {}) if isinstance(contract, dict) else {}
    pl = contract.get("placement", {}) if isinstance(contract, dict) else {}
    digest = rt.get("artifact_digest") or contract.get("runtime_artifact_digest", "")
    regions = [str(r) for r in (pl.get("regions") or []) if r]
    secrets = list(contract.get("required_secret_refs", []) or [])
    return WorkloadRequirements(
        cpu_architecture="x86_64",
        min_cpu=cpu,
        min_memory_mb=mem,
        gpu=GPURequirement(required=gpu),
        min_storage_gb=storage,
        runtime_artifact_digest=digest or None,
        required_secret_refs=secrets,
        placement=PlacementConstraints(allowed_regions=regions),
        reconnectable=True,
    )


def _telemetry_capacity_for_job(job) -> tuple:
    """Capacity confidence + artifact flag from worker telemetry.

    PA-9 (shim stage): jobs carrying a `worker_telemetry` mapping
    (heartbeat age, progress, preflight) drive readiness evidence;
    rows without it have unknown capacity and no artifact evidence.
    STALE/MISSING heartbeat degrades confidence (readiness UNKNOWN
    downstream) — it never reports healthy.
    """
    tele = (job or {}).get("worker_telemetry") or {}
    if not isinstance(tele, dict) or not tele:
        return 0.0, False, False
    try:
        from orchestrator.worker_telemetry import heartbeat_liveness
        live = heartbeat_liveness(tele.get("heartbeat"))
    except Exception:
        live = {"liveness": "UNKNOWN", "readiness": "UNKNOWN"}
    if live.get("liveness") == "FRESH":
        conf = float(tele.get("provisioning_success_rate", 0.9) or 0.9)
        return max(0.0, min(conf, 1.0)), True, False
    # STALE/MISSING/UNKNOWN: degraded, explicitly non-synthetic.
    return 0.2, False, False


# S16 plan-persistence seam (Track B1): planner step names → execution types.
_PLAN_STEP_TYPES = {
    "prechecking": "PRECHECK", "checkpointing": "CHECKPOINT",
    "persisting": "PERSIST", "provisioning": "PROVISION",
    "transferring": "TRANSFER", "restoring": "RESTORE",
    "fencing": "FENCE", "validating": "VALIDATE",
    "activating": "ACTIVATE", "finalizing": "FINALIZE",
}


def _admit_plan_to_store(plan_store, plan, log=None):
    """Admit a created plan to the Plan Store (best-effort, never raises).

    Execution-shaped doc so list_active/frontier logic applies; duplicate
    admission (retried tick) is tolerated. Enables crash visibility and
    the S16 pre-emption lookup; terminal marking happens at completion.
    """
    try:
        from datetime import timezone as _tz
        steps = []
        for s in getattr(plan, "steps", []) or []:
            name = getattr(s, "name", "unknown")
            steps.append({
                "step_id": f"{plan.migration_id[:8]}-{name}",
                "type": _PLAN_STEP_TYPES.get(str(name).lower(),
                                             str(name).upper()),
                "state": "PENDING",
                "operation_id": None,
                "estimated_seconds": getattr(s, "estimated_seconds", None),
            })
        plan_store.put_plan_doc({
            "plan_id": plan.plan_id,
            "execution_plan": decode(encode(plan)),
            "execution_epoch": plan.execution_epoch,
            "expires_at": plan.expires_at.isoformat(),
            "migration_id": plan.migration_id,
            "job_id": plan.job_id,
            "regime": getattr(getattr(plan, "regime", None), "value",
                              str(getattr(plan, "regime", ""))),
            "source_pool_id": getattr(plan, "source_pool_id", ""),
            "target_pool_id": getattr(plan, "target_pool_id", ""),
            "plan_hash": getattr(plan, "plan_hash", ""),
            "absolute_deadline": (plan.absolute_deadline.isoformat()
                                  if getattr(plan, "absolute_deadline", None)
                                  else None),
            "created_at": (getattr(plan, "created_at", None) or
                           datetime.now(_tz.utc)).isoformat()
            if isinstance(getattr(plan, "created_at", None), datetime)
            else datetime.now(_tz.utc).isoformat(),
            "steps": steps,
        })
    except KeyError:
        existing = plan_store.get_plan(plan.plan_id)
        if existing.get("plan_hash") != plan.plan_hash:
            raise RuntimeError("plan admission identity conflict")
    except Exception:
        raise
    return plan_store.get_plan(plan.plan_id)


def _mark_plan_outcome(plan_store, plan, outcome, log=None):
    """Stamp a terminal plan outcome (best-effort, never raises)."""
    try:
        plan_store.mark_plan_outcome(plan.plan_id, outcome)
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Plan outcome marking skipped for %s: %s",
                          getattr(plan, "plan_id", "?"), exc)
            except Exception:
                pass


def _measured_step_overrides(history, job_id, log=None) -> dict:
    """P95 measured durations from this job's migration history (Phase C).

    Returns {} when history is unavailable or too thin — the caller keeps
    guessed defaults. Measured values replace duration *estimates* only;
    the provider deadline is never altered here.
    """
    try:
        from orchestrator.recovery_feasibility import p95_step_estimates
        feedback = history.get_feedback_for_estimator(job_id)
        return p95_step_estimates(feedback)
    except Exception as exc:
        if log is not None:
            try:
                log.debug("P95 overrides unavailable for %s: %s", job_id, exc)
            except Exception:
                pass
        return {}


# Later pass: PENDING admission handling (invariants 1-3). Admitted jobs
# carry no PID/IP (spotctl run never asks); the loop provisions the
# initial worker and promotes PENDING -> RUNNING on live worker
# telemetry (FRESH heartbeat + pid). All facts are worker-observed,
# never user-supplied. Throttles keep failed admission from hot-looping.
PENDING_PROVISION_RETRY_SECONDS = 300.0
PENDING_TELEMETRY_POLL_SECONDS = 60.0


def _try_promote_pending_job(job, job_id, registry, transport_for, log=None):
    """Single telemetry promotion attempt. Returns True when promoted.

    Never raises: transport loss, stale heartbeats and CAS conflicts all
    degrade to a skip (caller throttles re-polls). FRESH heartbeat + pid
    is the only promotion evidence (invariant 4 extends to admission).
    """
    host = (job or {}).get("public_ip")
    if not host:
        return False
    try:
        from orchestrator.worker_telemetry import (
            WorkerTelemetryReader, heartbeat_liveness)
        reader = WorkerTelemetryReader(transport_for(host))
        hb = reader.read_heartbeat()
        live = heartbeat_liveness(hb)
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Job %s telemetry read failed: %s", job_id, exc)
            except Exception:
                pass
        return False
    if (live.get("liveness") != "FRESH" or not (hb or {}).get("pid")
            or hb.get("job_id") != job_id
            or hb.get("instance_id") != job.get("instance_id")
            or hb.get("execution_epoch") != job.get("execution_epoch")):
        return False
    try:
        pid = int(hb["pid"])
    except (TypeError, ValueError):
        return False
    attrs = {
        "public_ip": host,
        "pid": pid,
        "instance_id": hb.get("instance_id") or job.get("instance_id"),
        "worker_telemetry": {
            "liveness": "FRESH",
            "age_seconds": live.get("age_seconds"),
            "pid": pid,
        },
        "admission_status": "live",
    }
    try:
        registry.transition(job_id, "RUNNING", **attrs)
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Job %s promotion raced/skipped: %s", job_id, exc)
            except Exception:
                pass
        return False
    if log is not None:
        try:
            log.info("Job %s worker live (pid=%s); PENDING -> RUNNING",
                     job_id, pid)
        except Exception:
            pass
    return True


def _select_initial_pool(job, requirements, prices, candidate_pools,
                         compatibility_engine, placement_engine, log=None):
    """Cheapest placement-ranked COMPATIBLE pool for an admitted job.

    Readiness is not gated here — no worker exists yet to observe. The
    pick is compatibility ∩ placement order over contract regions with
    live price data. Returns the region key or None (with reason logged).
    """
    contract = (job or {}).get("workload_contract") or {}
    placement = contract.get("placement", {}) if isinstance(contract, dict) else {}
    wanted = placement.get("regions") or contract.get("regions") or []
    wanted = [str(r) for r in wanted if r]
    regions = [r for r in wanted if r in prices] or list(prices)
    compat = {}
    for r in regions:
        pool = candidate_pools.get(r)
        if pool is None:
            continue
        try:
            compat[r] = compatibility_engine.assess(requirements, pool)
        except Exception:
            continue
    eligible = [r for r in regions
                if r in compat
                and getattr(getattr(compat[r], "status", None),
                            "value", None) == "COMPATIBLE"]
    if not eligible:
        if log is not None:
            try:
                log.warning("Job %s admission: no COMPATIBLE pool in %s",
                            job.get("job_id"), regions)
            except Exception:
                pass
        return None
    try:
        inputs = []
        for r in eligible:
            a = compat[r]
            inputs.append(PlacementInput(
                pool_id=r,
                compatibility_status=a.status,
                # Pre-worker ordering only: eligibility was already decided
                # by the COMPATIBLE filter above (no worker exists to
                # observe). The live worker gates RUNNING via telemetry;
                # nothing here marks a pool READY as a fact.
                readiness_status=ReadinessStatus.READY,
                compatibility_dimensions={
                    d.dimension: (d.status.value == "COMPATIBLE")
                    for d in getattr(a, "dimensions", [])},
                readiness_checks=[],
                cost_analysis={"expected_total_cost": 1.0},
                risk_analysis={"interruption_probability": 0.0},
                topology={},
                capacity_confidence=0.5,
                pool_lifecycle=getattr(
                    candidate_pools.get(r), "lifecycle", "ACTIVE"),
            ))
        # NOTE: readiness_status mirrors compat ONLY to satisfy the input
        # schema; eligibility below uses compat alone, never readiness.
        rec = placement_engine.rank(inputs, regime="ARBITRAGE")
        if rec.selected_candidate_id in set(eligible):
            return rec.selected_candidate_id
    except Exception as exc:
        if log is not None:
            try:
                log.debug("Initial placement failed, first-compatible: %s", exc)
            except Exception:
                pass
    return eligible[0]



def quarantine_restart_frontiers(report, plan_store, registry, log):
    """Keep a recovered frontier inactive until its external receipts resolve."""
    frontiers = [*report.get("resumed", []), *report.get("unknown_unresolved", [])]
    for item in frontiers:
        doc = plan_store.get_plan(item["plan_id"])
        if not doc.get("execution_plan"):
            raise RuntimeError("active V2 plan lacks an executable body")
        log.error("Plan %s requires crash-phase reconciliation at %s",
                  item["plan_id"], item.get("resume_from_type", item.get("step_type")))
        if item.get("job_id"):
            current = registry.get(item["job_id"])
            if current.get("state") == "MIGRATING":
                registry.transition(item["job_id"], "RECONCILIATION_REQUIRED")
    for item in report.get("orphans", []):
        if not item.get("job_id"):
            continue
        current = registry.get(item["job_id"])
        if current.get("state") == "MIGRATING":
            log.error("Job %s references an absent migration plan %s",
                      item["job_id"], item.get("migration_id"))
            registry.transition(item["job_id"], "RECONCILIATION_REQUIRED")


def run_iteration(context):
    """One bounded production iteration, with injected dependencies and clock."""
    _cpr_lease = context.values['_cpr_lease']
    _ingestion = context.values['_ingestion']
    _max_replans = context.values['_max_replans']
    _pending_attempt = context.values['_pending_attempt']
    _persisted_pools = context.values['_persisted_pools']
    _replan_counts = context.values['_replan_counts']
    _replan_mod = context.values['_replan_mod']
    _seen_interruptions = context.values['_seen_interruptions']
    _tele_attempt = context.values['_tele_attempt']
    _transport_for = context.values['_transport_for']
    _wired_coordinator = context.values['_wired_coordinator']
    admission_provisioner = context.values['admission_provisioner']
    args = context.values['args']
    cfg = context.values['cfg']
    check_lease_for_iteration = context.values['check_lease_for_iteration']
    checkpoint_store = context.values['checkpoint_store']
    compatibility_engine = context.values['compatibility_engine']
    estimator = context.values['estimator']
    evaluator = context.values['evaluator']
    event_dispatcher = context.values['event_dispatcher']
    favorable_streaks = context.values['favorable_streaks']
    feasibility_engine = context.values['feasibility_engine']
    history = context.values['history']
    include_states = context.values['include_states']
    instance_type = context.values['instance_type']
    last_migration_ts = context.values['last_migration_ts']
    last_reconcile = context.values['last_reconcile']
    load_v2_baseline = context.values['load_v2_baseline']
    log = context.values['log']
    placement_engine = context.values['placement_engine']
    plan_store = context.values['plan_store']
    planner = context.values['planner']
    policy = context.values['policy']
    pool_store = context.values['pool_store']
    price_cache = context.values['price_cache']
    rate_limiter = context.values['rate_limiter']
    readiness_engine = context.values['readiness_engine']
    reconcile_interval = context.values['reconcile_interval']
    reconciliation = context.values['reconciliation']
    registry = context.values['registry']
    watcher = context.values['watcher']
    _lease_ok, _lease_action = check_lease_for_iteration(_cpr_lease) if args.migrate else (True, "observe")
    if not _lease_ok:
        log.error("Control-plane lease lost (%s); halting actuation",
                  _lease_action)
        return False
    now = context.wall_clock()

    if price_cache["data"] and now - price_cache["ts"] < PRICE_CACHE_TTL:
        prices = price_cache["data"]
    else:
        prices = watcher.poll()
        price_cache = {"ts": now, "data": prices}

    log.info("Prices: %s", {r: round(v["price"], 5) for r, v in prices.items()})

    if args.migrate and now - last_reconcile >= reconcile_interval:
        reconciliation.reconcile(ReconciliationTrigger.PERIODIC_SWEEP)
        last_reconcile = now

    jobs = []
    if args.multi_job:
        for st in include_states:
            jobs.extend(registry.list_by_state(st))
    else:
        jobs = [registry.get(args.job_id)]

    for job in jobs:
        job_id = job.get("job_id")
        current_region = job.get("region") or args.current_region
        if not job_id or not current_region:
            continue

        if job.get("state") and job.get("state") != "RUNNING":
            last_updated = job.get("last_updated")
            if last_updated:
                try:
                    updated_dt = datetime.fromisoformat(last_updated.replace("Z", ""))
                    updated_ts = updated_dt.timestamp()
                    if now - updated_ts > args.stuck_seconds:
                        log.warning("ALERT job %s stuck in %s for >%ss", job_id, job.get("state"), args.stuck_seconds)
                except Exception:
                    pass
            if job.get("state") == "PENDING":
                if not args.migrate:
                    continue
                # Later pass: admitted jobs are owned end-to-end.
                # Provisioned (has IP) -> telemetry promotion attempt.
                # Unprovisioned -> initial placement + provision.
                # Either way nothing migrates this tick.
                _handle_pending_job(
                    job, job_id, current_region,
                    prices=prices, instance_type=instance_type,
                    pool_store=pool_store,
                    compatibility_engine=compatibility_engine,
                    placement_engine=placement_engine,
                    provisioner=admission_provisioner,
                    rate_limiter=rate_limiter,
                    registry=registry,
                    transport_for=_transport_for,
                    pending_attempt=_pending_attempt,
                    tele_attempt=_tele_attempt,
                    now=now, log=log, launch_handler=context.values.get("launch_handler"),
                )
                continue
            continue

        workload_type = job.get("workload_type", "medium")
        execution_epoch = job.get("execution_epoch", 0)
        current_price = prices.get(current_region, {}).get("price", 0)
        candidate_prices = {r: p["price"] for r, p in prices.items() if r != current_region}
        interruption_risk = prices.get(current_region, {}).get("interruption_probability")
        if interruption_risk is None:
            log.warning("Job %s lacks interruption-risk evidence; skipping economics", job_id)
            continue

        observation = None
        if job.get("progress") is not None:
            observation = WorkloadObservation(
                job_id=job_id,
                execution_epoch=execution_epoch,
                progress=job.get("progress"),
                checkpoint_size_bytes=job.get("checkpoint_size_bytes"),
                checkpoint_duration_seconds=job.get("checkpoint_duration_seconds"),
                cpu_utilization=job.get("cpu_utilization"),
                memory_utilization=job.get("memory_utilization"),
                observed_at=datetime.utcnow(),
                observation_confidence=job.get("observation_confidence", 0.5),
            )

        workload_estimate = estimator.estimate(job_id, execution_epoch, workload_type, observation)

        # PA-10: requirements come from the job's workload contract
        # (spotctl admission), not a fixed fabrication.
        requirements = _requirements_for_job(job, log)

        # Candidate pools: registry definitions are authoritative;
        # per-tick construction only fills gaps for unregistered
        # regions, then persists them (never silently invents).
        from storage.pool_registry import (
            definition_from_pool, validate_pool_definition,
        )
        candidate_pools = {}
        definitions = {}
        for r, price in candidate_prices.items():
            pool_id = f"pool-{r}-{instance_type}"
            try:
                definition = pool_store.get_pool(pool_id)["definition"]
            except KeyError:
                continue
            if not re.fullmatch(r"sha256:[0-9a-f]{64}",
                                definition.get("runtime_artifact_digest") or ""):
                continue
            definitions[r] = definition
            pool = CandidatePool(pool_id=pool_id, provider=definition.get("provider", "aws"),
                account_id=definition.get("account_id", ""), region=definition["region"],
                availability_zone=definition.get("availability_zone", ""), instance_type=definition["instance_type"],
                architecture=definition.get("architecture", "x86_64"),
                runtime_profile=PoolRuntimeProfile(artifact_digest=definition["runtime_artifact_digest"],
                    architecture=definition.get("architecture", "x86_64")),
                capacity_profile=PoolCapacityProfile(max_instances=definition.get("max_instances", 1)))
            pool.lifecycle = definition.get("lifecycle", "ACTIVE")
            candidate_pools[r] = pool

        # Telemetry-driven readiness (PA-9): worker-reported evidence
        # when the job carries it; synthetic defaults otherwise,
        # explicitly flagged. STALE telemetry degrades (invariant 4).
        _tele_conf, _tele_artifact, _tele_synthetic = \
            _telemetry_capacity_for_job(job)
        if _tele_synthetic:
            log.debug("Job %s readiness evidence is synthetic "
                      "(no worker telemetry yet)", job_id)
        # Assess compatibility and readiness
        compat_assessments = {}
        ready_assessments = {}
        for r, pool in candidate_pools.items():
            compat = compatibility_engine.assess(requirements, pool)
            compat_assessments[r] = compat

            from orchestrator.readiness_evidence import readiness_arguments
            provider = context.values.get("readiness_evidence_provider")
            evidence = provider(pool.pool_id) if provider else definitions[r].get("readiness_evidence")
            ready = readiness_engine.assess(requirements, pool, **readiness_arguments(evidence))
            ready_assessments[r] = ready

        # Filter emergency-eligible pools
        emergency_pools = [
            r for r in candidate_pools
            if compat_assessments[r].status.value == "COMPATIBLE"
            and ready_assessments[r].status.value == "READY"
        ]

        stay_analysis = evaluator.evaluate_stay(current_price, workload_estimate, interruption_risk)
        candidate_analyses = []
        for r in emergency_pools:
            ca = evaluator.evaluate_candidate(
                current_price,
                prices[r]["price"],
                workload_estimate,
                interruption_risk,
                checkpoint_size_bytes=workload_estimate.checkpoint_size_estimate_bytes,
                checkpoint_duration_seconds=workload_estimate.checkpoint_duration_estimate_seconds,
            )
            ca.target_pool_id = r
            candidate_analyses.append(ca)

        regime = MigrationRegime.ARBITRAGE
        _active_interruption = None
        if args.check_spot_flag:
            source_ip = job.get("public_ip")
            if source_ip and _spot_flag_detected(source_ip, args.spot_flag_path, log):
                # Ingestion lane owns the event + deadline (§24.1, S21).
                # Presence-without-content degrades to now() as
                # effective_at; content carries the IMDS detected_at.
                _flag_doc = _ingestion.read_spot_flag(
                    source_ip, args.spot_flag_path, log)
                if _flag_doc is None:
                    from datetime import timezone as _tz
                    _flag_doc = {"detected_at": datetime.now(_tz.utc).isoformat(),
                                 "source": "flag-presence"}
                _evt = _ingestion.ingest_interruption(
                    _flag_doc, job_id=job_id,
                    execution_epoch=job.get("execution_epoch", 0),
                    instance_id=job.get("instance_id", ""),
                    source_region=current_region,
                    home_region=cfg.get("dynamodb_region"))
                if _evt is None:
                    log.warning("Job %s spot flag unreadable; staying in %s",
                                job_id, regime.value)
                elif _evt.event_id in _seen_interruptions:
                    log.info("Job %s duplicate interruption %s; already handled",
                             job_id, _evt.event_id)
                else:
                    _stale, _reason = _ingestion.is_stale_event(_evt, job)
                    if _stale:
                        log.warning("Job %s interruption %s dropped (%s)",
                                    job_id, _evt.event_id, _reason)
                    else:
                        try:
                            event_dispatcher.dispatch(_evt) if args.migrate else None
                        except Exception as exc:
                            log.warning("Interruption dispatch failed for %s: %s",
                                        job_id, exc)
                        _active_interruption = _evt
                        regime = MigrationRegime.EMERGENCY
                        try:
                            _lag = float((_evt.payload or {}).get(
                                "detection_lag_seconds", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            _lag = 0.0
                        log.warning("Job %s spot interruption %s; triggering "
                                    "EMERGENCY regime (detection_lag=%.1fs)",
                                    job_id, _evt.event_id, _lag)

        # Hysteresis bookkeeping (ADR-006): streak of economically
        # favorable observations per job; reset when no saving candidate.
        try:
            _best_saving = max(
                (c.cost_breakdown.expected_savings for c in candidate_analyses),
                default=0.0,
            )
        except Exception:
            _best_saving = 0.0
        if _best_saving > 0:
            favorable_streaks[job_id] = favorable_streaks.get(job_id, 0) + 1
        else:
            favorable_streaks[job_id] = 0
        _cooldown_cfg = policy.arbitrage.config.cooldown_seconds
        _last = last_migration_ts.get(job_id)
        _cooldown_left = max(0.0, _cooldown_cfg - (now - _last)) if _last else 0.0

        # Placement ranking (ADR-009): economics-free rank over eligible
        # candidates; policy keeps target authority but now decides over
        # placement-ordered lists so its [0] pick == placement top.
        # Previously placement_engine was built but never called and the
        # plan used candidate_analyses[0] (iteration order).
        if emergency_pools:
            try:
                _ca_by_pool = {c.target_pool_id: c for c in candidate_analyses}
                _placement_inputs = []
                for r in emergency_pools:
                    _compat_a = compat_assessments[r]
                    _ready_a = ready_assessments[r]
                    _placement_inputs.append(PlacementInput(
                        pool_id=r,
                        compatibility_status=_compat_a.status,
                        readiness_status=_ready_a.status,
                        compatibility_dimensions={
                            d.dimension: (d.status.value == "COMPATIBLE")
                            for d in getattr(_compat_a, "dimensions", [])},
                        readiness_checks=[
                            {"status": c.status.value}
                            for c in getattr(_ready_a, "checks", [])],
                        cost_analysis=_ca_by_pool.get(r),
                        risk_analysis={"interruption_probability": interruption_risk},
                        topology={},
                        capacity_confidence=float(
                            getattr(getattr(_ready_a, "capacity_evidence", None),
                                    "confidence", 0.5) or 0.5),
                        pool_lifecycle=getattr(
                            candidate_pools.get(r), "lifecycle", "ACTIVE"),
                    ))
                _rec = placement_engine.rank(
                    _placement_inputs, regime=regime.value)
                _order = [rc.pool_id for rc in _rec.ranked_candidates]
                _order_set = set(_order)
                emergency_pools = (
                    [r for r in _order if r in compat_assessments]
                    + [r for r in emergency_pools if r not in _order_set]
                )
                candidate_analyses.sort(
                    key=lambda c: emergency_pools.index(c.target_pool_id)
                    if c.target_pool_id in emergency_pools else len(emergency_pools))
                if _rec.selected_candidate_id is not None:
                    log.info("Job %s placement selected %s (%d candidates)",
                             job_id, _rec.selected_candidate_id,
                             len(_rec.ranked_candidates))
                else:
                    log.warning("Job %s placement selected none; "
                                "keeping prior order", job_id)
            except Exception as exc:
                log.warning("Job %s placement ranking failed, "
                            "keeping prior order: %s", job_id, exc)

        feasibility = None
        absolute_deadline = None
        if regime == MigrationRegime.EMERGENCY and emergency_pools:
            # Protocols §24.1: the deadline arrives computed from the
            # ingestion lane (Sprint 1); the presence-only fallback below only
            # covers presence-without-content notices. Window comes from
            # the frozen baseline; provider notice windows replace it.
            from orchestrator.deadlines import (
                compute_absolute_deadline, remaining_budget_seconds,
            )
            from datetime import timezone
            _ingested_deadline = None
            if _active_interruption is not None:
                _raw = (_active_interruption.payload or {}).get("absolute_deadline")
                try:
                    _ingested_deadline = datetime.fromisoformat(
                        str(_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    _ingested_deadline = None
            if _ingested_deadline is not None:
                absolute_deadline = _ingested_deadline
                _window = float((_active_interruption.payload or {}).get(
                    "window_seconds", 120.0))
                _deadline = remaining_budget_seconds(absolute_deadline) or _window
            else:
                try:
                    _baseline = load_v2_baseline()
                    _window = float(_baseline.get("execution", {}).get(
                        "emergency_window_seconds", 120.0))
                except Exception:
                    _window, _baseline = 120.0, None
                _detected_at = datetime.now(timezone.utc)
                absolute_deadline = compute_absolute_deadline(
                    _detected_at, _window, current_region,
                    emergency_pools[0], cfg.get("dynamodb_region"),
                    _baseline)
                _deadline = remaining_budget_seconds(absolute_deadline) or _window
            # Invariant 6: evaluate recovery feasibility independently
            # for EVERY eligible candidate; drop infeasible ones before
            # policy selection. Emergency target =
            # placement ∩ compatible ∩ ready ∩ recovery-feasible.
            # _steps shared (estimate-driven); compat/readiness differ.
            _plan_steps = build_emergency_plan_steps(
                workload_estimate,
                measured_overrides=_measured_step_overrides(
                    history, job_id, log),
            )
            _feas_by_pool: dict = {}
            for _r in emergency_pools:
                try:
                    _feas_by_pool[_r] = feasibility_engine.evaluate(
                        workload_estimate,
                        compat_assessments[_r],
                        ready_assessments[_r],
                        _deadline,
                        _plan_steps,
                    )
                except Exception as exc:
                    log.warning("Job %s feasibility failed for %s: %s",
                                job_id, _r, exc)
            _allow_cold = bool(getattr(
                getattr(policy, "recovery", None), "config", None)
                is not None and getattr(
                    policy.recovery.config, "allow_cold_restart", False))
            _survivors = []
            for _r in emergency_pools:  # keep placement order
                _f = _feas_by_pool.get(_r)
                _st = getattr(getattr(_f, "status", None), "value", None)
                if _st == "FEASIBLE":
                    _survivors.append(_r)
                elif _st == "INSUFFICIENT_EVIDENCE" and _allow_cold:
                    _survivors.append(_r)
                else:
                    log.info("Job %s emergency candidate %s excluded (%s)",
                             job_id, _r,
                             _st or "no feasibility")
            if not _survivors:
                log.warning("Job %s no feasible emergency candidate; "
                            "ABANDON", job_id)
                feasibility = None
                emergency_pools = []
                candidate_analyses = []
            else:
                emergency_pools = _survivors
                candidate_analyses = [
                    c for c in candidate_analyses
                    if c.target_pool_id in _survivors]
                feasibility = _feas_by_pool[_survivors[0]]
            # Invariant 5: durability comes from the authoritative
            # checkpoint store, never from migration intent. Missing or
            # uncertain => False => cold-restart/ABANDON branches.
            _checkpoint_durable = query_checkpoint_durable(
                checkpoint_store, job_id, log)
            if not _checkpoint_durable:
                log.info("Job %s has no durable checkpoint; "
                         "recovery will consider cold-restart/ABANDON",
                         job_id)
            decision = policy.decide(
                regime, stay_analysis, candidate_analyses, workload_estimate,
                [compat_assessments[r] for r in emergency_pools],
                [ready_assessments[r] for r in emergency_pools],
                recovery_feasibility=feasibility,
                recovery_cost=None,
                checkpoint_durable=_checkpoint_durable,
                job_context={"workload_type": workload_type},
                risk_context={"interruption_probability": interruption_risk},
            )
            # Emergency decisions carry assessment pool_ids
            # ("pool-<region>-<type>"); the loop keys assessments by
            # region. Remap so downstream lookups never KeyError.
            if (decision.target_candidate_id is not None
                    and decision.target_candidate_id not in compat_assessments):
                for _rk, _ca in compat_assessments.items():
                    if getattr(_ca, "pool_id", None) == decision.target_candidate_id:
                        decision.target_candidate_id = _rk
                        break
        else:
            decision = policy.decide(
                regime, stay_analysis, candidate_analyses, workload_estimate,
                [compat_assessments[r] for r in emergency_pools],
                [ready_assessments[r] for r in emergency_pools],
                job_context={"workload_type": workload_type},
                risk_context={"interruption_probability": interruption_risk},
                favorable_observations=favorable_streaks.get(job_id, 0),
                cooldown_remaining_seconds=_cooldown_left,
            )

        log.info("Job %s decision: action=%s target=%s reason=%s regime=%s",
                 job_id, decision.decision.value, decision.target_candidate_id, decision.reason, regime.value)

        if decision.decision.value not in ("MIGRATE", "RECOVER"):
            continue

        last_ts = last_migration_ts.get(job_id)
        if regime != MigrationRegime.EMERGENCY and last_ts and (now - last_ts) < args.cooldown_seconds:
            log.info("Job %s cooldown active; skipping migration", job_id)
            continue

        if not decision.target_candidate_id:
            log.warning("Job %s has no target candidate; skipping", job_id)
            continue

        if args.migrate:
            migration_limiter = context.values.get("emergency_rate_limiter", rate_limiter) if regime == MigrationRegime.EMERGENCY else rate_limiter
            if not migration_limiter.acquire(timeout=0):
                stats = migration_limiter.get_stats()
                log.info("Job %s migration rate-limited (active=%s/%s)", job_id, stats["active_count"], stats["max_concurrent"])
                get_metrics().inc("migration_rate_limited_total")
                continue

            try:
                source_pool_id = job.get("pool_id", f"source-{current_region}")
                target_pool_id = decision.target_candidate_id

                compat = compat_assessments[target_pool_id]
                ready = ready_assessments[target_pool_id]

                # Economics provenance for the chosen target (not just
                # iteration-order [0]): match the analysis whose
                # target_pool_id == decision target.
                _chosen_analysis = next(
                    (c for c in candidate_analyses
                     if getattr(c, "target_pool_id", None) == target_pool_id),
                    candidate_analyses[0] if candidate_analyses else None,
                )
                plan = planner.create_plan(
                    job_id=job_id,
                    execution_epoch=execution_epoch,
                    regime=regime,
                    policy_decision=decision,
                    source_pool_id=source_pool_id,
                    target_pool_id=target_pool_id,
                    workload_estimate=workload_estimate,
                    recovery_feasibility=feasibility if regime == MigrationRegime.EMERGENCY else None,
                    stay_analysis=stay_analysis,
                    candidate_analysis=_chosen_analysis,
                    absolute_deadline=absolute_deadline,
                    source_identity=job,
                )
                # Fencing needs the source execution identity at run time.
                plan.pid = job.get("pid")
                plan.source_host = job.get("public_ip")

                history.record_start(plan, decision, stay_analysis, feasibility if regime == MigrationRegime.EMERGENCY else None)
                # S16 seam: admit the plan so crash recovery and the
                # pre-emption lookup can see it (best-effort).
                _admit_plan_to_store(plan_store, plan, log)

                def on_state_change(old, new):
                    log.info("Migration %s state: %s -> %s", plan.migration_id, old.value, new.value)

                executor = _wired_coordinator()
                execution_state = executor.execute_plan(plan, on_state_change)
                for op in execution_state.operations:
                    duration = (op.completed_at - op.started_at).total_seconds() if op.completed_at else 0
                    history.record_step(plan.migration_id, op.step_name, op.status, op.result, op.error, duration)

                if execution_state.current_state.value == "SUCCESS":
                    history.record_completion(plan.migration_id, "SUCCESS")
                    _mark_plan_outcome(plan_store, plan, "SUCCEEDED", log)
                    last_migration_ts[job_id] = context.wall_clock()
                    if _active_interruption is not None:
                        _seen_interruptions.add(_active_interruption.event_id)
                elif execution_state.current_state.value == "ABORTED":
                    history.record_completion(plan.migration_id, "ABORTED")
                    _mark_plan_outcome(plan_store, plan, "ABORTED", log)
                elif execution_state.current_state.value == "SUPERSEDED":
                    history.record_completion(plan.migration_id, "SUPERSEDED")
                    _mark_plan_outcome(plan_store, plan, "SUPERSEDED", log)
                else:
                    history.record_completion(plan.migration_id, "FAILED", str(execution_state))
                    _mark_plan_outcome(plan_store, plan, "FAILED", log) if registry.get(job_id).get("state") == "RUNNING" else None
                    # Replan path (§25/S15/S04): pre-fence failures with
                    # a replan signal get bounded successor attempts.
                    # Post-fence outcomes never reach here with a signal
                    # (forward recovery owns them).
                    _signal = _replan_mod.replan_signal_for(execution_state)
                    if _signal is not None and _replan_mod.budget_remaining(
                            _replan_counts, plan.migration_id, _max_replans):
                        _attempt = _replan_mod.note_replan(
                            _replan_counts, plan.migration_id)
                        log.info("Job %s replan attempt %d (%s)",
                                 job_id, _attempt, _signal)
                        try:
                            _fresh_estimate = estimator.estimate(
                                job_id, execution_epoch, workload_type, None)
                            _successor = planner.create_successor_plan(
                                plan, f"{_signal}#{_attempt}", _fresh_estimate)
                            _successor.pid = job.get("pid")
                            _successor.source_host = job.get("public_ip")
                            history.record_start(
                                _successor, decision, stay_analysis, None)
                            _admit_plan_to_store(plan_store, _successor, log)
                            _succ_state = executor.execute_plan(
                                _successor, on_state_change)
                            if _succ_state.current_state.value == "SUCCESS":
                                history.record_completion(
                                    _successor.migration_id, "SUCCESS")
                                _mark_plan_outcome(
                                    plan_store, _successor, "SUCCEEDED", log)
                                last_migration_ts[job_id] = time.time()
                            else:
                                history.record_completion(
                                    _successor.migration_id, "FAILED",
                                    str(_succ_state))
                                _mark_plan_outcome(
                                    plan_store, _successor, "FAILED", log)
                        except Exception as exc:
                            log.warning("Job %s replan attempt failed: %s",
                                        job_id, exc)

            finally:
                migration_limiter.release()
        else:
            log.info("Job %s migration suggested (dry-run). Use --migrate to execute.", job_id)

    context.values.update(price_cache=price_cache, last_reconcile=last_reconcile)
    return True

def main():
    parser = argparse.ArgumentParser(description="V2 Orchestrator: Observe -> Estimate -> Compatibility -> Feasibility -> Economics -> Policy -> Plan -> Execute")
    parser.add_argument("--job-id", help="Job ID in the registry (required in single-job mode)")
    parser.add_argument("--current-region", help="Current region of the job (single-job mode)")
    parser.add_argument("--regions", help="Comma-separated regions to poll; if omitted uses runtime config candidate_regions")
    parser.add_argument("--instance-type", help="Instance type; defaults to runtime config instance_type")
    parser.add_argument("--interval", type=int, default=MIGRATION_POLL_INTERVAL, help=f"Poll interval seconds (default {MIGRATION_POLL_INTERVAL})")
    parser.add_argument("--migrate", action="store_true", help="If set, execute migrations when decided")
    parser.add_argument("--cooldown-seconds", type=int, default=COOLDOWN_SECONDS, help=f"Min seconds between migrations for a job (default {COOLDOWN_SECONDS//3600}h)")
    parser.add_argument("--health-port", type=int, default=HEALTH_CHECK_PORT, help=f"Health check HTTP port (default {HEALTH_CHECK_PORT})")
    parser.add_argument("--multi-job", action="store_true", help="Enable multi-job mode (iterate over all PENDING/RUNNING jobs)")
    parser.add_argument("--states", default="PENDING,RUNNING", help="Comma-separated states to include in multi-job mode (default PENDING,RUNNING)")
    parser.add_argument("--stuck-seconds", type=int, default=STUCK_JOB_THRESHOLD, help=f"Alert if job state is unchanged longer than this (default {STUCK_JOB_THRESHOLD//60}min)")
    parser.add_argument("--spot-flag-path", default="/opt/job_workspace/flags/spot_interrupt", help="Worker spot interruption flag path")
    parser.add_argument("--check-spot-flag", action="store_true", help="Check worker for spot interruption flag and trigger emergency recovery")
    parser.add_argument("--max-migrations-per-hour", type=int, default=MAX_MIGRATIONS_PER_HOUR, help=f"Max migrations per hour (default {MAX_MIGRATIONS_PER_HOUR})")
    parser.add_argument("--max-concurrent-migrations", type=int, default=MAX_CONCURRENT_MIGRATIONS, help=f"Max concurrent migrations (default {MAX_CONCURRENT_MIGRATIONS})")
    parser.add_argument("--reconcile-interval", type=int, default=300, help="Reconciliation sweep interval seconds (default 300)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    load_logging_config()
    log = logging.getLogger("orchestrator.main")
    cfg = load_runtime_config()

    regions = []
    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    elif cfg.get("raw", {}).get("candidate_regions"):
        regions = cfg["raw"]["candidate_regions"]
    else:
        raise SystemExit("No regions provided (pass --regions or set candidate_regions in config/runtime.yaml)")

    instance_type = args.instance_type or cfg.get("instance_type")
    if not instance_type:
        raise SystemExit("instance_type not set (pass --instance-type or set in config/runtime.yaml)")

    if cfg.get("registry_backend") != "dynamo":
        raise SystemExit("V2 requires the DynamoDB registry backend")
    registry = DynamoRegistry(cfg["dynamodb_table"], region_name=cfg.get("dynamodb_region"))
    log.info("Using DynamoDB registry: table=%s region=%s", cfg["dynamodb_table"], cfg.get("dynamodb_region"))

    # Initialize V2 components
    watcher = SpotPriceWatcher(regions=regions, instance_type=instance_type)
    
    estimator = WorkloadEstimator(EstimatorConfig())
    compatibility_engine = CompatibilityEngine()
    readiness_engine = ReadinessEngine()
    placement_policy = PlacementPolicy.load_from_file("config/placement_policy.yaml")
    placement_engine = PlacementEngine(placement_policy)
    feasibility_engine = RecoveryFeasibilityEngine()
    evaluator = CostRiskEvaluator()
    try:
        from orchestrator.config_loader import load_v2_baseline
        _base = load_v2_baseline()
        _pol = _base.get("policy", {})
        _sj = _base.get("short_job", {})
        policy = PolicyEngine(ArbitragePolicyConfig(
            min_savings_margin=float(_pol.get("min_savings_margin", 0.10)),
            cooldown_seconds=float(_pol.get("cooldown_seconds", 900)),
            min_favorable_observations=int(_pol.get("min_favorable_observations", 3)),
            max_completion_overhead_ratio=float(_pol.get("max_completion_overhead", 0.20)),
            short_runtime_seconds=float(_sj.get("short_runtime_seconds", 900)),
            medium_runtime_seconds=float(_sj.get("medium_runtime_seconds", 3600)),
            medium_net_benefit_multiplier=float(_sj.get("medium_required_net_benefit_multiplier", 2.0)),
            medium_required_savings_fraction=float(_sj.get("medium_required_savings_fraction", 0.20)),
            policy_version="v3",
        ))
        log.info("Loaded V2 baseline policy config (ADR-006..008)")
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"V2 baseline load failed: {exc}") from exc
    planner = MigrationPlanner()
    
    # Sprint 2 (Track C2): shared worker transports + validation context
    # bus. L1/L2 probes resolve per-migration context published by the
    # Coordinator's execution_context_sink after restore; without context
    # they report inconclusive, never stub-pass (I7/I15).
    from worker.transport import SSHWorkerTransport
    from orchestrator.utils import SSHClient
    from orchestrator import validator_probes as _probes
    _transports: dict[str, SSHWorkerTransport] = {}

    if args.migrate and (not cfg.get("ssh_private_key_path") or not cfg.get("ssh_known_hosts_path")):
        raise SystemExit("migration requires SSH_PRIVATE_KEY_PATH and SSH_KNOWN_HOSTS_PATH")

    def _transport_for(host: str) -> SSHWorkerTransport:
        if host not in _transports:
            client = SSHClient(host, user="ubuntu", key_path=cfg.get("ssh_private_key_path"),
                known_hosts_path=cfg.get("ssh_known_hosts_path"), strict=True)
            _transports[host] = SSHWorkerTransport(host, ssh_client=client, strict_host_keys=True)
        return _transports[host]

    _validation_contexts: dict[str, dict] = {}

    def _publish_validation_context(ctx: dict):
        try:
            _validation_contexts[ctx["migration_id"]] = dict(ctx)
        except Exception as exc:
            log.warning("validation context publish failed: %s", exc)

    _probe_context = _probes.make_context_provider(_validation_contexts)

    validator = Validator(default_tolerance={"version": "batch-state-v2", "metrics": {"progress_delta": {"tolerance_type": "RELATIVE", "value": .05}}},
        infra_probe=_probes.make_infra_probe(_transport_for, _probe_context),
        app_probe=_probes.make_app_probe(_transport_for, _probe_context),
    )
    history = MigrationHistory()

    # Track B1: production stores share the authoritative registry resource.
    # Resource initialization failure never selects an in-memory backend.
    # Table names match infra/aws/dynamodb.tf.
    dynamodb_resource = registry.dynamodb
    log.info("V2 execution stores using DynamoDB in %s", cfg.get("dynamodb_region"))
    from storage.operation_store import OperationStore
    operation_store = OperationStore(dynamodb_resource=dynamodb_resource)
    plan_store = PlanStore(dynamodb_resource=dynamodb_resource)
    checkpoint_store = CheckpointStore(dynamodb_resource=dynamodb_resource)
    audit_store = AuditStore(dynamodb_resource=dynamodb_resource)
    history_store = HistoryStore(dynamodb_resource=dynamodb_resource)
    # Wire durable history: MigrationHistory was built pre-store (line ~250
    # with storage=None for import-order reasons); late-bind here so
    # record_start/completion actually persist instead of living only in
    # the in-process list. Without this every migration outcome is lost
    # on restart (Risk Model feedback loop starved).
    history.storage = history_store
    pool_store = PoolRegistryStore(dynamodb_resource=dynamodb_resource)
    if args.migrate:
        from pathlib import Path
        from storage.config_store import ConfigStore
        config_store = ConfigStore(dynamodb_resource=dynamodb_resource)
        config_store.publish("baseline", _base["runtime"]["engine_version"], _base)
        placement_doc = yaml.safe_load(Path("config/placement_policy.yaml").read_text())
        config_store.publish("placement", placement_doc["version"], placement_doc)
    from storage.s3_manager import S3Manager
    checkpoint_bucket = cfg.get("checkpoint_bucket")
    checkpoint_kms = cfg.get("checkpoint_kms_key_arn") or os.getenv("CHECKPOINT_KMS_KEY_ARN")
    if args.migrate and (not checkpoint_bucket or not checkpoint_kms):
        raise SystemExit("migration requires checkpoint bucket and KMS key ARN")
    checkpoint_storage = S3Manager(checkpoint_bucket, kms_key_id=checkpoint_kms) if checkpoint_bucket else None
    provisioner = Provisioner(key_name=cfg.get("ssh_key_name"),
        max_spot_price=cfg.get("max_spot_price"), operation_store=operation_store, regions=regions)
    checkpoint_mgr = CheckpointManager(storage=checkpoint_storage, checkpoint_store=checkpoint_store)
    cleanup = CleanupExecutor(provisioner=provisioner, storage_manager=checkpoint_storage,
        checkpoint_manager=checkpoint_mgr)
    # pool_store persists candidate pool definitions from the per-job loop
    # below (once per pool_id, version 1); capacity/readiness stay live.
    # Phase B: seed from config/pools.json when present (`spotctl runtime
    # publish` writes it) so registry rows — not per-tick invention —
    # are authoritative from process start.
    _seeded_pool_ids: set[str] = set()
    try:
        from storage.pool_registry import load_definitions_file
        import os as _os
        _pools_file = _os.path.join("config", "pools.json")
        for _pid, _def in (load_definitions_file(_pools_file).items() if args.migrate else []):
            try:
                pool_store.put_pool(_pid, dict(_def), version=1)
                _seeded_pool_ids.add(_pid)
            except Exception:
                continue
        if _seeded_pool_ids:
            log.info("Pool store seeded with %d definitions from %s",
                     len(_seeded_pool_ids), _pools_file)
    except Exception as exc:
        log.debug("Pool seed skipped: %s", exc)
    log.info("Pool store initialized: table=%s",
             getattr(pool_store, "table_name", "pool_registry"))
    from orchestrator.event_ledger import EventLedger
    event_ledger = EventLedger(dynamodb_resource=dynamodb_resource)
    # Sprint 1 (S14/S21): in-process dispatcher + interruption ingestion
    # lane. Recon subscriptions fan out to the ReconciliationManager once
    # constructed below (late-bound via set_reconciliation_manager).
    from orchestrator import interruption_ingestion as _ingestion
    event_dispatcher = _ingestion.build_dispatcher(event_ledger)
    _seen_interruptions: set[str] = set()
    # Pool definitions are durable config (storage/pool_registry.py):
    # persist each candidate pool once per process (version 1). Capacity
    # and readiness stay live observations, never stored here.
    _persisted_pools: set[str] = set()
    _persisted_pools.update(_seeded_pool_ids)

    def _wired_coordinator():
        """Coordinator bound to real stores, transports, and fence hooks."""
        from orchestrator.criu_handlers import (
            make_dump_handler_for, make_restore_handler_for,
            make_fence_hooks_for, make_restore_status_provider_for,
        )
        s3 = checkpoint_storage
        transfer = TransferManager(storage=s3)

        def _pid_resolver(plan):
            try:
                job = registry.get(plan.job_id)
            except Exception:
                job = {}
            return job.get("public_ip"), job.get("pid")

        checkpoint = CheckpointManager(storage=s3,
            dump_handler=make_dump_handler_for(_transport_for),
            restore_handler=make_restore_handler_for(_transport_for),
            checkpoint_store=checkpoint_store,
            status_provider=make_restore_status_provider_for(
                _transport_for, operation_store, plan_store))
        provisioner_cfg = provisioner
        import json, shlex
        from orchestrator.worker_controller import make_controller_fence_hooks, file_refusal
        def channel_for(host):
            def channel(command):
                result = _transport_for(host).run_command(
                    "sudo -n python3 -I /opt/spot-worker-control/control_entry.py --command "
                    + shlex.quote(json.dumps(command)), timeout=30)
                if result["returncode"] != 0:
                    raise RuntimeError("worker controller outcome uncertain")
                return json.loads(result["stdout"])
            return channel
        hooks = make_controller_fence_hooks(channel_for,
            lambda plan: {"host": plan.source_host, "pid": plan.pid, "instance_id": plan.source_instance_id},
            command_metadata=lambda plan: {"process_start": plan.source_process_start,
                                          "lease": {"owner": _cpr_lease.owner, "generation": _cpr_lease.generation}},
            refusal_sink=lambda refusal: file_refusal(refusal, reconciliation,
                job_id=refusal.get("job_id", ""), migration_id=refusal.get("migration_id")))

        def _pool_definition_for(target_pool_id: str):
            """Resolve a target pool definition for execution (PA-8).

            Plan targets are region keys (or full pool_ids after the
            emergency remap); the store keys full pool_ids. Try both.
            None => execution refuses the missing pool definition.
            """
            candidates = [target_pool_id]
            try:
                candidates.append(f"pool-{target_pool_id}-{instance_type}")
            except Exception:
                pass
            for pid in candidates:
                try:
                    doc = pool_store.get_pool(pid)
                except Exception:
                    continue
                definition = (doc or {}).get("definition", {})
                if definition:
                    return definition
            return None

        def _snapshot_provider(job_id: str, phase: str):
            """Workload-evidence snapshots for L3 validation.

            Built from registry-side execution telemetry (progress,
            utilization). Restored execution must present the same shaped
            evidence; identical pre/post passes tolerance, drift fails it.
            """
            from orchestrator.worker_telemetry import WorkerTelemetryReader
            if phase == "pre":
                host = registry.get(job_id).get("public_ip")
            else:
                contexts = [ctx for ctx in _validation_contexts.values() if ctx.get("job_id") == job_id]
                host = contexts[-1].get("target_host") if contexts else None
            if not host:
                return None
            report = WorkerTelemetryReader(_transport_for(host)).read_progress()
            if not report:
                return None
            raw = report.get("raw", {})
            progress = report.get("progress")
            if progress is None:
                progress = raw.get("tick", raw.get("iterations_done"))
            if progress is None:
                return None
            return {"progress_delta": progress}

        def _target_contained(state):
            if not state.target_host or not state.target_pid or not state.target_process_start:
                return False
            response = _transport_for(state.target_host).run_command(
                "cd /opt/job_workspace && python3 -I "
                "/opt/spot-worker-control/control_entry.py process-control inspect "
                f"--pid {int(state.target_pid)} --start {shlex.quote(state.target_process_start)}",
                timeout=15)
            if response["returncode"] != 0:
                return False
            try:
                receipt = json.loads(response["stdout"])
            except ValueError:
                return False
            return receipt.get("same_process") is True and receipt.get("contained") is True

        from orchestrator.worker_execution import WorkerExecution, CheckpointDataPath
        from storage.pool_reservations import PoolReservations
        target_worker = WorkerExecution(_transport_for, guard=_cpr_lease.assert_fresh,
            controller_channel_factory=channel_for,
            lease_proof=lambda: {"owner": _cpr_lease.owner, "generation": _cpr_lease.generation})
        return MigrationCoordinator(
            registry=registry,
            provisioner=provisioner_cfg,
            checkpoint_manager=checkpoint,
            transfer_manager=transfer,
            validator=validator,
            cleanup_executor=CleanupExecutor(
                provisioner=provisioner_cfg, storage_manager=s3,
                checkpoint_manager=checkpoint),
            plan_store=plan_store,
            checkpoint_store=checkpoint_store,
            audit_store=audit_store,
            fence_invalidate=hooks["invalidate"],
            fence_terminate=hooks["terminate"],
            fence_verify=hooks["verify"],
            snapshot_provider=_snapshot_provider,
            execution_context_sink=_publish_validation_context,
            pool_definition_provider=_pool_definition_for,
            operation_store=operation_store, actuation_guard=_cpr_lease.assert_fresh,
            activation_handler=target_worker.activate,
            rollback_source=WorkerExecution(_transport_for, guard=_cpr_lease.assert_fresh).resume_source,
            checkpoint_data_path=CheckpointDataPath(_transport_for, s3, checkpoint_store),
            pool_reservations=PoolReservations(dynamodb_resource=dynamodb_resource),
            target_containment_probe=_target_contained,
        )

    # NOTE: only the `_wired_coordinator()` instance below executes plans;
    # a second plain instance here would be dead construction (vulture).
    reconciliation = ReconciliationManager(
        registry=registry,
        infrastructure_monitor=None,
        cleanup_executor=cleanup,
    )
    event_dispatcher.set_reconciliation_manager(reconciliation)

    # Sprint 4 (§14.3/S09): crash-recovery bootstrap — lease → in-flight →
    # UNKNOWN resolution → budget → frontier → sweep → resume. Report-only:
    # resumed plans are logged, never auto-executed (step-resume execution
    # is deferred); halt on lease loss or unreadable stores. The report
    # NEVER actuates by itself.
    import socket as _socket
    from orchestrator import bootstrap as _bootstrap_mod
    from orchestrator.control_plane_lease import (
        ControlPlaneLease, LeaseStore, check_lease_for_iteration,
    )
    _lease_store = LeaseStore(dynamodb_resource=dynamodb_resource)
    _cpr_lease = ControlPlaneLease(
        _lease_store,
        owner=f"cpr-{_socket.gethostname()}-{os.getpid()}")
    _coordinator = _wired_coordinator()
    _status_probe_targets = (operation_store, _coordinator.provisioner, _coordinator.checkpoint_manager, _coordinator.transfer_manager)

    def _resolve_operation_status(op_id):
        for _ex in _status_probe_targets:
            _probe = getattr(_ex, "get_operation_status", None)
            if not callable(_probe):
                continue
            try:
                _rep = _probe(op_id)
            except Exception:
                continue
            _outcome, _, _ = MigrationCoordinator._probe_outcome(_rep)
            if _outcome != "UNKNOWN":
                return _outcome
        return "UNKNOWN"

    _boot_report = _bootstrap_mod.bootstrap_recovery(
        lease=_cpr_lease,
        plan_store=plan_store,
        active_refs=_bootstrap_mod.collect_active_refs(registry),
        resolve_operation=_resolve_operation_status) if args.migrate else {
        "halted": False, "abandoned": [], "unknown_unresolved": [], "resumed": [], "orphans": []}
    if _boot_report["halted"]:
        log.error("Bootstrap halted (%s); refusing to actuate",
                  _boot_report["halt_reason"])
        raise SystemExit(1)
    if args.migrate:
        _cpr_lease.start()
    for _ab in _boot_report["abandoned"]:
        log.warning("Bootstrap abandoned plan %s (%s); routing job %s to recovery",
                    _ab.get("plan_id"), _ab.get("reason"), _ab.get("job_id"))
        if _ab.get("job_id"):
            try:
                registry.transition(_ab["job_id"], "RECOVERY_REQUIRED")
            except Exception as exc:
                log.warning("Abandoned-plan job routing failed for %s: %s",
                            _ab.get("job_id"), exc)
    for _uu in _bootstrap_mod.fence_adjacent_unresolved(_boot_report):
        log.warning("Bootstrap unresolved %s op %s; gating on reconciliation",
                    _uu.get("step_type"), _uu.get("operation_id"))
        try:
            reconciliation.report_event({
                "event_type": "FENCE_UNCONFIRMED",
                "job_id": _uu.get("job_id", ""),
                "migration_id": _uu.get("migration_id"),
                "execution_epoch": 0,
                **{k: v for k, v in _uu.items()
                   if k not in ("event_type", "job_id", "migration_id")},
            })
        except Exception as exc:
            log.warning("Fence-uncertainty finding filing failed: %s", exc)
    # A persisted SUCCEEDED phase may have lost its context write, and an
    # ISSUED phase may have run. Keep the frontier inactive until receipts
    # resolve instead of replaying external operations.
    quarantine_restart_frontiers(_boot_report, plan_store, registry, log)
    if _boot_report["orphans"]:
        log.warning("Bootstrap orphans (%d) feed the periodic sweep: %s",
                    len(_boot_report["orphans"]), _boot_report["orphans"])

    if not args.multi_job:
        if not args.job_id or not args.current_region:
            raise SystemExit("Single-job mode requires --job-id and --current-region")
    else:
        if cfg.get("registry_backend") != "dynamo":
            raise SystemExit("Multi-job mode requires DynamoDB backend")

    last_migration_ts = {}
    # Sprint 3 (§25/S15): bounded successor attempts per migration attempt
    # (same migration_id, fresh evidence each link; durable chain-walk
    # budget deferred to Plan Store wiring, Track B1).
    from orchestrator import replan as _replan_mod
    from orchestrator.config_loader import load_v2_baseline as _load_baseline
    _replan_counts: dict[str, int] = {}
    try:
        _max_replans = _replan_mod.max_replans_from_baseline(_load_baseline())
    except SystemExit:
        raise
    except Exception:
        _max_replans = _replan_mod.DEFAULT_MAX_REPLANS
    log.info("Replan budget: max_replans=%s", _max_replans)
    favorable_streaks: dict[str, int] = {}
    price_cache = {"ts": 0, "data": None}
    last_reconcile = 0
    reconcile_interval = args.reconcile_interval

    rate_limiter = TokenBucketRateLimiter(
        max_per_hour=args.max_migrations_per_hour,
        max_concurrent=args.max_concurrent_migrations,
        min_interval_seconds=MIGRATION_BACKOFF_SECONDS
    )
    log.info(
        "Rate limiter configured: max_per_hour=%s max_concurrent=%s min_interval=%ss",
        args.max_migrations_per_hour,
        args.max_concurrent_migrations,
        MIGRATION_BACKOFF_SECONDS,
    )
    # Admission and migration provisioning require authoritative pool definitions.
    admission_provisioner = provisioner
    # Later pass throttles (in-memory; restart resets to retry-soon):
    # provision attempts per job, telemetry polls per job.
    _pending_attempt: dict[str, float] = {}
    _tele_attempt: dict[str, float] = {}

    health_server = start_health_server(port=args.health_port)
    log.info(
        "Starting V2 orchestrator loop | multi_job=%s job=%s interval=%ss migrate=%s health_port=%s",
        args.multi_job,
        args.job_id,
        args.interval,
        args.migrate,
        args.health_port,
    )

    include_states = [s.strip() for s in args.states.split(",") if s.strip()] if args.states else ["PENDING", "RUNNING"]

    from orchestrator.runtime_context import RuntimeContext
    from orchestrator.worker_execution import WorkerExecution
    worker_execution = WorkerExecution(_transport_for, guard=_cpr_lease.assert_fresh)
    context = RuntimeContext({
        "launch_handler": worker_execution.launch,
        "emergency_rate_limiter": TokenBucketRateLimiter(max_per_hour=120, max_concurrent=args.max_concurrent_migrations, min_interval_seconds=0),
        '_cpr_lease': _cpr_lease,
        '_ingestion': _ingestion,
        '_max_replans': _max_replans,
        '_pending_attempt': _pending_attempt,
        '_persisted_pools': _persisted_pools,
        '_replan_counts': _replan_counts,
        '_replan_mod': _replan_mod,
        '_seen_interruptions': _seen_interruptions,
        '_tele_attempt': _tele_attempt,
        '_transport_for': _transport_for,
        '_wired_coordinator': _wired_coordinator,
        'admission_provisioner': admission_provisioner,
        'args': args,
        'cfg': cfg,
        'check_lease_for_iteration': check_lease_for_iteration,
        'checkpoint_store': checkpoint_store,
        'compatibility_engine': compatibility_engine,
        'estimator': estimator,
        'evaluator': evaluator,
        'event_dispatcher': event_dispatcher,
        'favorable_streaks': favorable_streaks,
        'feasibility_engine': feasibility_engine,
        'history': history,
        'include_states': include_states,
        'instance_type': instance_type,
        'last_migration_ts': last_migration_ts,
        'last_reconcile': last_reconcile,
        'load_v2_baseline': lambda: _base,
        'log': log,
        'placement_engine': placement_engine,
        'plan_store': plan_store,
        'planner': planner,
        'policy': policy,
        'pool_store': pool_store,
        'price_cache': price_cache,
        'rate_limiter': rate_limiter,
        'readiness_engine': readiness_engine,
        'reconcile_interval': reconcile_interval,
        'reconciliation': reconciliation,
        'registry': registry,
        'watcher': watcher,
    })
    if args.migrate:
        _cpr_lease.start()
    try:
        while run_iteration(context):
            time.sleep(args.interval)
    finally:
        if args.migrate:
            _cpr_lease.release()
        health_server.shutdown()
        health_server.server_close()
        for transport in _transports.values():
            transport.close()


if __name__ == "__main__":
    main()
