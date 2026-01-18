# orchestrator/main.py
import argparse
import logging
import logging.config
import time
import yaml
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from orchestrator.watcher import SpotPriceWatcher
from orchestrator.decision_engine import DecisionEngine
from orchestrator.migrator import Migrator
from orchestrator.config_loader import load_runtime_config
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
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format, *args):
        # suppress default stdout logging
        return


def start_health_server(port=8080):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Orchestrator main loop: poll -> decide -> (optional) migrate.")
    parser.add_argument("--job-id", help="Job ID in the registry (required in single-job mode)")
    parser.add_argument("--current-region", help="Current region of the job (single-job mode)")
    parser.add_argument("--regions", help="Comma-separated regions to poll; if omitted uses runtime config candidate_regions")
    parser.add_argument("--instance-type", help="Instance type; defaults to runtime config instance_type")
    parser.add_argument("--policy", default="orchestrator/sla_policy.yaml", help="SLA policy path")
    parser.add_argument("--registry-path", default="storage/job_registry.json", help="Path to job registry JSON")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval seconds (default 60)")
    parser.add_argument("--migrate", action="store_true", help="If set, trigger migrator when decision is MIGRATE")
    parser.add_argument("--cooldown-seconds", type=int, default=10800, help="Min seconds between migrations for a job (default 3h)")
    parser.add_argument("--target-ip", help="Optional target worker IP to skip prompt")
    parser.add_argument("--auto-provision", action="store_true", help="Auto-provision target worker (no manual IP prompt)")
    parser.add_argument("--target-region", help="Override target region (otherwise decision target)")
    parser.add_argument("--target-ami-id", help="Override target AMI ID")
    parser.add_argument("--target-sg-id", help="Override target security group ID")
    parser.add_argument("--max-spot-price", help="Override max spot price for provisioning")
    parser.add_argument("--health-port", type=int, default=8080, help="Health check HTTP port (default 8080)")
    parser.add_argument("--multi-job", action="store_true", help="Enable multi-job mode (iterate over all RUNNING jobs)")
    parser.add_argument("--states", default="RUNNING", help="Comma-separated states to include in multi-job mode (default RUNNING)")
    args = parser.parse_args()

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

    watcher = SpotPriceWatcher(regions=regions, instance_type=instance_type)
    engine = DecisionEngine(args.policy)
    # Select registry backend
    if cfg.get("registry_backend") == "dynamo" and cfg.get("dynamodb_table"):
        registry = DynamoRegistry(cfg["dynamodb_table"], region_name=cfg.get("dynamodb_region"))
        log.info("Using DynamoDB registry: table=%s region=%s", cfg["dynamodb_table"], cfg.get("dynamodb_region"))
    else:
        registry = JobRegistry(args.registry_path)
        log.info("Using JSON registry: %s", args.registry_path)
    migrator = Migrator(registry)

    # Validate mode
    if not args.multi_job:
        if not args.job_id or not args.current_region:
            raise SystemExit("Single-job mode requires --job-id and --current-region")
    else:
        if cfg.get("registry_backend") != "dynamo":
            raise SystemExit("Multi-job mode requires DynamoDB backend")

    # Per-job cooldown tracker
    last_migration_ts = {}
    price_cache = {"ts": 0, "data": None}
    price_cache_ttl = 30  # seconds

    health_server = start_health_server(port=args.health_port)
    log.info(
        "Starting orchestrator loop | multi_job=%s job=%s interval=%ss migrate=%s health_port=%s",
        args.multi_job,
        args.job_id,
        args.interval,
        args.migrate,
        args.health_port,
    )

    include_states = [s.strip() for s in args.states.split(",") if s.strip()] if args.states else ["RUNNING"]

    while True:
        now = time.time()
        if price_cache["data"] and now - price_cache["ts"] < price_cache_ttl:
            prices = price_cache["data"]
        else:
            prices = watcher.poll()
            price_cache = {"ts": now, "data": prices}

        log.info("Prices: %s", {r: round(v["price"], 5) for r, v in prices.items()})

        # Determine jobs to process
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

            decision = engine.evaluate(prices, current_region, job=job)
            log.info("Job %s decision: action=%s target=%s reason=%s", job_id, decision.action, decision.target_region, decision.reason)

            if decision.action != "MIGRATE":
                continue

            # Cooldown check per job
            last_ts = last_migration_ts.get(job_id)
            if last_ts and (now - last_ts) < args.cooldown_seconds:
                log.info("Job %s cooldown active; skipping migration (remaining %ss)", job_id, int(args.cooldown_seconds - (now - last_ts)))
                continue

            if args.migrate:
                target_region = args.target_region or decision.target_region
                provision_overrides = {
                    "ami_id": args.target_ami_id,
                    "security_group_id": args.target_sg_id,
                    "max_spot_price": args.max_spot_price,
                    "instance_type": instance_type,
                    "ssh_key_name": cfg.get("ssh_key_name"),
                }
                migrator.migrate(
                    job_id,
                    target_region,
                    target_ip=args.target_ip,
                    autoprovision=args.auto_provision or cfg.get("auto_provision"),
                    provision_overrides=provision_overrides,
                )
                last_migration_ts[job_id] = time.time()
            else:
                log.info("Job %s migration suggested (dry-run). Use --migrate to execute.", job_id)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()

