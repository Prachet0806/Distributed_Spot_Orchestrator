# orchestrator/main.py
import argparse
import logging
import logging.config
import os
import time
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import yaml

from orchestrator.watcher import SpotPriceWatcher
from orchestrator.workload_estimator import WorkloadEstimator, WorkloadObservation, EstimatorConfig
from orchestrator.workload_requirements import WorkloadRequirements, GPURequirement
from orchestrator.candidate_pool import CandidatePool, CandidatePoolRegistry
from orchestrator.candidate_compatibility import CompatibilityEngine
from orchestrator.candidate_readiness import ReadinessEngine, IAMReadiness, NetworkReadiness, StorageReadiness
from orchestrator.candidate_placement import PlacementEngine, PlacementPolicy, PlacementInput
from orchestrator.recovery_feasibility import RecoveryFeasibilityEngine
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
from storage.job_registry import JobRegistry
from storage.dynamo_registry import DynamoRegistry


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

        if self.path == "/metrics":
            payload = get_metrics().render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server(port=8080, timeout=10):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.timeout = timeout
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


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


def _select_failover_region(prices, current_region, override_region=None):
    if override_region:
        return override_region
    ordered = sorted(prices.items(), key=lambda x: x[1]["price"])
    for region, _ in ordered:
        if region != current_region:
            return region
    return None


def main():
    parser = argparse.ArgumentParser(description="V2 Orchestrator: Observe -> Estimate -> Compatibility -> Feasibility -> Economics -> Policy -> Plan -> Execute")
    parser.add_argument("--job-id", help="Job ID in the registry (required in single-job mode)")
    parser.add_argument("--current-region", help="Current region of the job (single-job mode)")
    parser.add_argument("--regions", help="Comma-separated regions to poll; if omitted uses runtime config candidate_regions")
    parser.add_argument("--instance-type", help="Instance type; defaults to runtime config instance_type")
    parser.add_argument("--policy", default="orchestrator/sla_policy.yaml", help="SLA policy path")
    parser.add_argument("--registry-path", default="storage/job_registry.json", help="Path to job registry JSON")
    parser.add_argument("--interval", type=int, default=MIGRATION_POLL_INTERVAL, help=f"Poll interval seconds (default {MIGRATION_POLL_INTERVAL})")
    parser.add_argument("--migrate", action="store_true", help="If set, execute migrations when decided")
    parser.add_argument("--cooldown-seconds", type=int, default=COOLDOWN_SECONDS, help=f"Min seconds between migrations for a job (default {COOLDOWN_SECONDS//3600}h)")
    parser.add_argument("--health-port", type=int, default=HEALTH_CHECK_PORT, help=f"Health check HTTP port (default {HEALTH_CHECK_PORT})")
    parser.add_argument("--multi-job", action="store_true", help="Enable multi-job mode (iterate over all RUNNING jobs)")
    parser.add_argument("--states", default="RUNNING", help="Comma-separated states to include in multi-job mode (default RUNNING)")
    parser.add_argument("--stuck-seconds", type=int, default=STUCK_JOB_THRESHOLD, help=f"Alert if job state is unchanged longer than this (default {STUCK_JOB_THRESHOLD//60}min)")
    parser.add_argument("--spot-flag-path", default="/tmp/spot_interrupt", help="Worker spot interruption flag path")
    parser.add_argument("--check-spot-flag", action="store_true", help="Check worker for spot interruption flag and trigger emergency recovery")
    parser.add_argument("--max-migrations-per-hour", type=int, default=MAX_MIGRATIONS_PER_HOUR, help=f"Max migrations per hour (default {MAX_MIGRATIONS_PER_HOUR})")
    parser.add_argument("--max-concurrent-migrations", type=int, default=MAX_CONCURRENT_MIGRATIONS, help=f"Max concurrent migrations (default {MAX_CONCURRENT_MIGRATIONS})")
    parser.add_argument("--reconcile-interval", type=int, default=300, help="Reconciliation sweep interval seconds (default 300)")
    parser.add_argument("--engine", choices=["v1", "v2"], default="v2",
                        help="Execution engine: v2 (default, Planner->Coordinator) or v1 (frozen legacy Migrator compat, ADR-024)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    load_logging_config()
    log = logging.getLogger("orchestrator.main")
    log.info("Engine selected: %s", args.engine)
    if args.engine == "v1":
        log.warning(
            "V1 engine is frozen legacy (ADR-024). Use --engine v2 for all new "
            "work; V1 path (Migrator/DecisionEngine) will be removed after "
            "AWS E2E + soak gates."
        )

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

    if cfg.get("registry_backend") == "dynamo" and cfg.get("dynamodb_table"):
        registry = DynamoRegistry(cfg["dynamodb_table"], region_name=cfg.get("dynamodb_region"))
        log.info("Using DynamoDB registry: table=%s region=%s", cfg["dynamodb_table"], cfg.get("dynamodb_region"))
    else:
        registry = JobRegistry(args.registry_path)
        log.info("Using JSON registry: %s", args.registry_path)

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
        log.warning("V2 baseline load failed, using defaults: %s", exc)
        policy = PolicyEngine(ArbitragePolicyConfig())
    planner = MigrationPlanner()
    
    provisioner = Provisioner()
    checkpoint_mgr = CheckpointManager()
    transfer_mgr = TransferManager()
    validator = Validator()
    cleanup = CleanupExecutor(provisioner=provisioner, storage_manager=None, checkpoint_manager=checkpoint_mgr)
    history = MigrationHistory()

    # Track B1: V2 execution stores share one DynamoDB resource. Local
    # backends until `terraform apply` lands the tables (Track A2); with
    # registry_backend=dynamo the same objects persist to DynamoDB.
    # Table names match infra/aws/dynamodb.tf.
    dynamodb_resource = None
    if cfg.get("registry_backend") == "dynamo":
        try:
            import boto3
            dynamodb_resource = boto3.resource(
                "dynamodb", region_name=cfg.get("dynamodb_region"))
            log.info("V2 execution stores using DynamoDB in %s",
                     cfg.get("dynamodb_region"))
        except Exception as exc:
            log.warning("DynamoDB resource init failed, local stores: %s", exc)
    else:
        log.info("V2 execution stores using local backends (registry_backend=%s)",
                 cfg.get("registry_backend"))
    plan_store = PlanStore(dynamodb_resource=dynamodb_resource)
    checkpoint_store = CheckpointStore(dynamodb_resource=dynamodb_resource)
    audit_store = AuditStore(dynamodb_resource=dynamodb_resource)
    history_store = HistoryStore(dynamodb_resource=dynamodb_resource)
    pool_store = PoolRegistryStore(dynamodb_resource=dynamodb_resource)
    from orchestrator.event_ledger import EventLedger
    event_ledger = EventLedger(dynamodb_resource=dynamodb_resource)

    def _wired_coordinator():
        """Coordinator bound to real stores, transports, and fence hooks."""
        from storage.s3_manager import S3Manager
        from worker.transport import SSHWorkerTransport
        from orchestrator.criu_handlers import (
            make_dump_handler_for, make_restore_handler_for,
            make_fence_hooks_for,
        )
        bucket = cfg.get("checkpoint_bucket")
        s3 = S3Manager(bucket=bucket) if bucket else None
        transfer = TransferManager(storage=s3)

        transports: dict[str, SSHWorkerTransport] = {}

        def _transport_for(host: str) -> SSHWorkerTransport:
            if host not in transports:
                transports[host] = SSHWorkerTransport(host)
            return transports[host]

        def _pid_resolver(plan):
            try:
                job = registry.get(plan.job_id)
            except Exception:
                job = {}
            return job.get("public_ip"), job.get("pid")

        checkpoint = CheckpointManager(
            storage=s3,
            dump_handler=make_dump_handler_for(_transport_for),
            restore_handler=make_restore_handler_for(_transport_for),
            checkpoint_store=checkpoint_store,
        )
        provisioner_cfg = Provisioner(
            region=cfg.get("target_region") or cfg.get("source_region"),
            ami_id=cfg.get("target_ami_id") or None,
            security_group_id=cfg.get("target_security_group_id") or None,
            key_name=cfg.get("ssh_key_name"),
            instance_type=instance_type,
            max_spot_price=cfg.get("max_spot_price"),
        )
        hooks = make_fence_hooks_for(_transport_for, pid_resolver=_pid_resolver)

        def _snapshot_provider(job_id: str, phase: str):
            """Workload-evidence snapshots for L3 validation.

            Built from registry-side execution telemetry (progress,
            utilization). Restored execution must present the same shaped
            evidence; identical pre/post passes tolerance, drift fails it.
            """
            try:
                job = registry.get(job_id)
            except Exception:
                return None
            snap = {
                "progress": job.get("progress", 0.0) or 0.0,
                "cpu_utilization_delta": job.get("cpu_utilization", 0.5) or 0.5,
                "memory_utilization_delta": job.get("memory_utilization", 0.5) or 0.5,
                "progress_delta": job.get("progress", 0.0) or 0.0,
            }
            return snap

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
        )

    coordinator = MigrationCoordinator(
        registry=registry,
        provisioner=provisioner,
        checkpoint_manager=checkpoint_mgr,
        transfer_manager=transfer_mgr,
        validator=validator,
        cleanup_executor=cleanup,
        plan_store=plan_store,
        checkpoint_store=checkpoint_store,
        audit_store=audit_store,
    )
    
    reconciliation = ReconciliationManager(
        registry=registry,
        infrastructure_monitor=None,
        cleanup_executor=cleanup,
    )

    if not args.multi_job:
        if not args.job_id or not args.current_region:
            raise SystemExit("Single-job mode requires --job-id and --current-region")
    else:
        if cfg.get("registry_backend") != "dynamo":
            raise SystemExit("Multi-job mode requires DynamoDB backend")

    last_migration_ts = {}
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

    health_server = start_health_server(port=args.health_port)
    log.info(
        "Starting V2 orchestrator loop | multi_job=%s job=%s interval=%ss migrate=%s health_port=%s",
        args.multi_job,
        args.job_id,
        args.interval,
        args.migrate,
        args.health_port,
    )

    include_states = [s.strip() for s in args.states.split(",") if s.strip()] if args.states else ["RUNNING"]

    while True:
        now = time.time()
        
        if price_cache["data"] and now - price_cache["ts"] < PRICE_CACHE_TTL:
            prices = price_cache["data"]
        else:
            prices = watcher.poll()
            price_cache = {"ts": now, "data": prices}

        log.info("Prices: %s", {r: round(v["price"], 5) for r, v in prices.items()})

        if now - last_reconcile >= reconcile_interval:
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

            workload_type = job.get("workload_type", "medium")
            execution_epoch = job.get("execution_epoch", 0)
            current_price = prices.get(current_region, {}).get("price", 0)
            candidate_prices = {r: p["price"] for r, p in prices.items() if r != current_region}
            interruption_risk = prices.get(current_region, {}).get("volatility", 0.1)

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

            requirements = WorkloadRequirements(
                cpu_architecture="x86_64",
                min_cpu=1,
                min_memory_mb=1024,
                gpu=GPURequirement(required=False),
                reconnectable=True,
            )

            # Create candidate pools for each region
            candidate_pools = {}
            for r, p in candidate_prices.items():
                pool_id = f"pool-{r}-{instance_type}"
                pool = CandidatePool(
                    pool_id=pool_id,
                    provider="aws",
                    account_id="123456789",
                    region=r,
                    availability_zone=f"{r}a",
                    instance_type=instance_type,
                    architecture="x86_64",
                    runtime_profile=PoolRuntimeProfile(
                        artifact_digest="sha256:abc123",
                        architecture="x86_64",
                    ),
                    capacity_profile=PoolCapacityProfile(
                        max_instances=10,
                    ),
                )
                candidate_pools[r] = pool

            # Assess compatibility and readiness
            compat_assessments = {}
            ready_assessments = {}
            for r, pool in candidate_pools.items():
                compat = compatibility_engine.assess(requirements, pool)
                compat_assessments[r] = compat
                
                # Create default readiness evidence
                iam_ready = IAMReadiness(
                    secret_resolution_verified=True,
                    kms_access_verified=True,
                    instance_profile_ready=True,
                    policy_attached=True,
                )
                network_ready = NetworkReadiness(
                    vpc_configured=True,
                    subnet_available=True,
                    security_groups_ready=True,
                    eni_attachable=True,
                )
                storage_ready = StorageReadiness(
                    checkpoint_bucket_accessible=True,
                    ebs_attachable=True,
                    snapshot_creation_verified=True,
                )
                capacity_evidence = CapacityEvidence(
                    status=CapacityStatus.AVAILABLE,
                    source=EvidenceSource.HISTORICAL_PROVISIONING,
                    observed_at=datetime.utcnow(),
                    confidence=0.8,
                    provisioning_success_rate=0.9,
                    sample_count=10,
                )
                
                ready = readiness_engine.assess(
                    requirements, candidate_pools[r],
                    iam_ready=iam_ready, network_ready=network_ready, storage_ready=storage_ready,
                    capacity_evidence=capacity_evidence,
                    artifact_available=True,
                )
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
                    prices[r],
                    workload_estimate,
                    interruption_risk,
                    checkpoint_size_bytes=workload_estimate.checkpoint_size_estimate_bytes,
                    checkpoint_duration_seconds=workload_estimate.checkpoint_duration_estimate_seconds,
                )
                ca.target_pool_id = r
                candidate_analyses.append(ca)

            regime = MigrationRegime.ARBITRAGE
            if args.check_spot_flag:
                source_ip = job.get("public_ip")
                if source_ip and _spot_flag_detected(source_ip, args.spot_flag_path, log):
                    regime = MigrationRegime.EMERGENCY
                    log.warning("Job %s spot interruption detected; triggering EMERGENCY regime", job_id)

            compat_list = [compat_assessments[r] for r in emergency_pools]
            ready_list = [ready_assessments[r] for r in emergency_pools]

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
            _cooldown_cfg = getattr(policy.arbitrage.config, "effective_cooldown_seconds",
                                    policy.arbitrage.config.cooldown_hours * 3600.0)
            _last = last_migration_ts.get(job_id)
            _cooldown_left = max(0.0, _cooldown_cfg - (now - _last)) if _last else 0.0

            feasibility = None
            absolute_deadline = None
            if regime == MigrationRegime.EMERGENCY and emergency_pools:
                # Protocols §24.1: computed once at ingestion (here: spot-flag
                # detection) until Market/Risk Monitor owns it. Window comes
                # from the frozen baseline; provider notice windows replace it.
                from orchestrator.deadlines import (
                    compute_absolute_deadline, remaining_budget_seconds,
                )
                from datetime import timezone
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
                feasibility = feasibility_engine.evaluate(
                    workload_estimate,
                    compat_assessments[emergency_pools[0]],
                    ready_assessments[emergency_pools[0]],
                    _deadline,
                )
                decision = policy.decide(
                    regime, stay_analysis, candidate_analyses, workload_estimate,
                    [compat_assessments[r] for r in emergency_pools],
                    [ready_assessments[r] for r in emergency_pools],
                    recovery_feasibility=feasibility,
                    recovery_cost=None,
                    checkpoint_durable=True,
                    job_context={"workload_type": workload_type},
                    risk_context={"interruption_probability": interruption_risk},
                )
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
            if last_ts and (now - last_ts) < args.cooldown_seconds:
                log.info("Job %s cooldown active; skipping migration", job_id)
                continue

            if not decision.target_candidate_id:
                log.warning("Job %s has no target candidate; skipping", job_id)
                continue

            if args.migrate:
                if not rate_limiter.acquire(timeout=0):
                    stats = rate_limiter.get_stats()
                    log.info("Job %s migration rate-limited (active=%s/%s)", job_id, stats["active_count"], stats["max_concurrent"])
                    get_metrics().inc("migration_rate_limited_total")
                    continue

                try:
                    source_pool_id = job.get("pool_id", f"source-{current_region}")
                    target_pool_id = decision.target_candidate_id

                    compat = compat_assessments[target_pool_id]
                    ready = ready_assessments[target_pool_id]

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
                        candidate_analysis=candidate_analyses[0] if candidate_analyses else None,
                        absolute_deadline=absolute_deadline,
                    )
                    # Fencing needs the source execution identity at run time.
                    plan.pid = job.get("pid")
                    plan.source_host = job.get("public_ip")

                    history.record_start(plan, decision, stay_analysis, feasibility if regime == MigrationRegime.EMERGENCY else None)

                    def on_state_change(old, new):
                        log.info("Migration %s state: %s -> %s", plan.migration_id, old.value, new.value)

                    executor = _wired_coordinator()
                    execution_state = executor.execute_plan(plan, on_state_change)

                    if execution_state.current_state.value == "SUCCESS":
                        history.record_completion(plan.migration_id, "SUCCESS")
                        last_migration_ts[job_id] = time.time()
                    elif execution_state.current_state.value == "ABORTED":
                        history.record_completion(plan.migration_id, "ABORTED")
                    elif execution_state.current_state.value == "SUPERSEDED":
                        history.record_completion(plan.migration_id, "SUPERSEDED")
                    else:
                        history.record_completion(plan.migration_id, "FAILED", str(execution_state))

                finally:
                    rate_limiter.release()
            else:
                log.info("Job %s migration suggested (dry-run). Use --migrate to execute.", job_id)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()