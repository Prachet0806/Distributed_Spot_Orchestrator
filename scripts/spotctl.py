# scripts/spotctl.py — thin user CLI (Phase A shim, baseline-locked).
"""Single entry point for the user workflow. v1 is a thin shim over the
existing registry/history mechanisms — no PID/IP/AMI/Dynamo knowledge
required on the normal path:

    python scripts/spotctl.py run --name risk-sim --script ./monte_carlo.py
    python scripts/spotctl.py status --name risk-sim
    python scripts/spotctl.py history --name risk-sim
    python scripts/spotctl.py cost --name risk-sim

The normal `run` path NEVER accepts --public-ip/--pid/--ami (invariant
1+2): it admits a PENDING job carrying a workload contract. PID/host
discovery (PA-6/9), pool-backed provisioning (PA-7/8) and telemetry
READINESS replace the shim internals progressively without changing
this UX.
"""
import argparse
import sys

sys.path.insert(0, ".")


def _parse_memory_mb(raw):
    if raw is None:
        return 1024
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lower().replace(" ", "")
    for suffix, mult in (("gib", 1024), ("gb", 1000), ("mib", 1), ("mb", 1)):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s))


def _registry(args):
    if args.backend == "dynamo":
        if not args.table or not args.region:
            raise SystemExit("Dynamo backend requires --table and --region")
        from storage.dynamo_registry import DynamoRegistry
        return DynamoRegistry(args.table, region_name=args.region)
    from storage.job_registry import JobRegistry
    return JobRegistry(args.json_path)


def _history_store(args):
    dynamodb_resource = None
    if args.backend == "dynamo":
        try:
            import boto3
            dynamodb_resource = boto3.resource("dynamodb", region_name=args.region)
        except Exception as exc:
            raise SystemExit(f"DynamoDB resource init failed: {exc}")
    from storage.history_store import HistoryStore
    return HistoryStore(
        table_name=args.history_table, dynamodb_resource=dynamodb_resource)


def cmd_run(args):
    from orchestrator.admission import JobAdmissionManager, WorkloadContract
    reg = _registry(args)
    regions = [r.strip() for r in (args.regions or "").split(",") if r.strip()]
    if not regions and args.region:
        regions = [args.region]
    contract = WorkloadContract(
        name=args.name, script=args.script, cpu=args.cpu,
        memory_mb=_parse_memory_mb(args.memory), gpu=bool(args.gpu),
        storage_gb=args.storage_gb, regions=tuple(regions),
        workload_type=args.workload_type,
        max_recovery_time_s=args.max_recovery_time,
    )
    manager = JobAdmissionManager(default_regions=[args.region] if args.region else [])
    result = manager.admit(contract, reg, job_id=args.job_id)
    if result.verdict == "REJECTED":
        raise SystemExit(f"admission rejected: {result.reason}")
    print(f"admitted {result.job_id} ({result.verdict}; contract v1; worker provisioning: shim-v1 manual)")
    print(f"status:  python scripts/spotctl.py status --name {result.job_id} --backend {args.backend}"
          + (f" --table {args.table} --region {args.region}" if args.backend == "dynamo" else f" --json-path {args.json_path}"))
    return result.job_id


def cmd_status(args):
    reg = _registry(args)
    try:
        job = reg.get(args.name)
    except Exception as exc:
        raise SystemExit(f"status failed: {exc}")
    print(f"job:     {job.get('job_id', args.name)}")
    print(f"state:   {job.get('state')}")
    print(f"region:  {job.get('region')}")
    print(f"epoch:   {job.get('execution_epoch', 0)}")
    print(f"active:  {job.get('active_migration_id')}")
    print(f"progress:{job.get('progress')}")
    contract = job.get("workload_contract") or {}
    if contract:
        res = contract.get("resources", {})
        print(f"contract: cpu={res.get('cpu')} mem_mb={res.get('memory_mb')} gpu={res.get('gpu')}")


def _history_rows(args):
    store = _history_store(args)
    try:
        return store.list_by_job(args.name)
    except Exception as exc:
        raise SystemExit(f"history failed: {exc}")


def cmd_history(args):
    rows = _history_rows(args)
    if not rows:
        print(f"no migrations recorded for {args.name}")
        return
    for doc in sorted(rows, key=lambda d: d.get("started_at", "")):
        outcome = doc.get("outcome", "?")
        if isinstance(outcome, dict):
            outcome = outcome.get("value", "?")
        print(f"--- {doc.get('migration_id')} [{outcome}] regime={doc.get('regime')}")
        print(f"    {doc.get('source_candidate_id')} -> {doc.get('target_candidate_id')}"
              f" reason={doc.get('failure_reason') or (doc.get('policy_decision') or {}).get('reason', '')}")
        steps = doc.get("steps") or []
        if steps:
            parts = ", ".join(
                f"{s.get('step')}:{s.get('duration_seconds')}s/{s.get('status')}" for s in steps)
            print(f"    steps: {parts}")
        print(f"    total: {doc.get('total_duration_seconds')}s")


def cmd_cost(args):
    rows = _history_rows(args)
    if not rows:
        print(f"no migrations recorded for {args.name}")
        return
    n_ok = sum(1 for d in rows if str(d.get("outcome", "")).endswith("SUCCESS"))
    tot_dur = sum(float(d.get("total_duration_seconds") or 0) for d in rows)
    est_save = sum(float((d.get("cost_analysis") or {}).get("expected_savings") or 0) for d in rows)
    print(f"job: {args.name} migrations={len(rows)} success={n_ok} "
          f"success_rate={n_ok / max(len(rows), 1):.2f}")
    print(f"total_migration_time_s={tot_dur:.1f} estimated_savings={est_save:.4f}")
    print("(actual spend: billing validation scheduled, Track E3)")


def cmd_runtime_publish(args):
    """Register a runtime artifact digest with pool definitions (Phase B).

    AMI baking itself stays out-of-band for v1: --ami mappings record
    which image carries the digest per region. Drift becomes a
    deterministic compatibility failure (_check_runtime digest mismatch),
    never a migration-time surprise.
    """
    from storage.pool_registry import (
        load_definitions_file, save_definitions_file,
        validate_pool_definition,
    )
    amis = {}
    for item in args.ami:
        if "=" not in item:
            raise SystemExit(f"--ami must be REGION=AMI, got {item!r}")
        region, ami = item.split("=", 1)
        amis[region.strip()] = ami.strip()
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    if not regions:
        raise SystemExit("--regions is required")
    defs = load_definitions_file(args.pools_file)
    for region in regions:
        pool_id = f"pool-{region}-{args.instance_type}"
        cur = dict(defs.get(pool_id, {}))
        cur.update({
            "pool_id": pool_id,
            "provider": cur.get("provider", "aws"),
            "region": region,
            "availability_zone": cur.get("availability_zone", f"{region}a"),
            "instance_type": args.instance_type,
            "architecture": cur.get("architecture", "x86_64"),
            "ami_id": amis.get(region, cur.get("ami_id", "")),
            "security_group_id": cur.get("security_group_id", args.security_group),
            "iam_profile": cur.get("iam_profile", args.iam_profile),
            "runtime_artifact_digest": args.digest,
            "criu_version": args.criu or cur.get("criu_version", ""),
            "kernel_version": args.kernel or cur.get("kernel_version", ""),
            "lifecycle": cur.get("lifecycle", "ACTIVE"),
            "max_concurrent_migrations": cur.get("max_concurrent_migrations", 2),
        })
        errs = validate_pool_definition(cur)
        if errs:
            print(f"note: {pool_id} incomplete ({'; '.join(errs)}) — "
                  f"fill --ami/--security-group for a fully authoritative pool")
        defs[pool_id] = cur
    save_definitions_file(args.pools_file, defs)
    print(f"published {args.digest} to {len(regions)} pool(s) in {args.pools_file}")
    print("drift policy: digest mismatch → INCOMPATIBLE at compatibility check")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spotctl", description="Spot arbitrage user CLI (thin shim v1)")
    ap.add_argument("--backend", choices=["dynamo", "json"], default="json")
    ap.add_argument("--table", default=None, help="DynamoDB registry table")
    ap.add_argument("--region", default=None, help="DynamoDB region")
    ap.add_argument("--json-path", default="storage/job_registry.json")
    ap.add_argument("--history-table", default="spot_arbitrage_migration_history")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Admit a job (no PID/IP/AMI flags on this path)")
    r.add_argument("--name", required=True, help="Job name (job-id derived if --job-id absent)")
    r.add_argument("--job-id", default=None)
    r.add_argument("--script", required=True)
    r.add_argument("--cpu", type=int, default=1)
    r.add_argument("--memory", default="1GiB")
    r.add_argument("--gpu", action="store_true")
    r.add_argument("--storage-gb", type=int, default=10)
    r.add_argument("--regions", default="")
    r.add_argument("--workload-type", default="batch")
    r.add_argument("--max-recovery-time", type=int, default=100)

    s = sub.add_parser("status", help="Show job state")
    s.add_argument("--name", required=True)

    h = sub.add_parser("history", help="Show migration history")
    h.add_argument("--name", required=True)

    c = sub.add_parser("cost", help="Show savings summary")
    c.add_argument("--name", required=True)

    rt = sub.add_parser("runtime", help="Runtime artifact operations")
    rtsub = rt.add_subparsers(dest="runtime_cmd", required=True)
    pub = rtsub.add_parser("publish", help="Register a runtime artifact with pools")
    pub.add_argument("--digest", required=True, help="Runtime artifact digest (sha256:...)")
    pub.add_argument("--regions", required=True, help="Comma-separated regions")
    pub.add_argument("--instance-type", default="t3.micro")
    pub.add_argument("--ami", action="append", default=[],
                     help="REGION=AMI mapping (repeatable); AMI baking itself stays out-of-band v1")
    pub.add_argument("--security-group", default="")
    pub.add_argument("--iam-profile", default="")
    pub.add_argument("--criu", default="")
    pub.add_argument("--kernel", default="")
    pub.add_argument("--python", default="")
    pub.add_argument("--pools-file", default="config/pools.json")

    args = ap.parse_args(argv)
    if args.cmd == "runtime":
        cmd_runtime_publish(args)
        return
    {"run": cmd_run, "status": cmd_status, "history": cmd_history,
     "cost": cmd_cost}[args.cmd](args)


if __name__ == "__main__":
    main()
