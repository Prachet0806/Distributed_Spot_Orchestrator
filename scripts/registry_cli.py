import argparse
from storage.dynamo_registry import DynamoRegistry
from storage.job_registry import JobRegistry


def ensure_dynamo(table, region, op_fn):
    reg = DynamoRegistry(table, region_name=region)
    return op_fn(reg)


def ensure_json(path, op_fn):
    reg = JobRegistry(path)
    return op_fn(reg)


def main():
    parser = argparse.ArgumentParser(description="Registry CLI (DynamoDB or JSON) for creating/updating jobs.")
    parser.add_argument("--backend", choices=["dynamo", "json"], required=True, help="Registry backend to use")
    parser.add_argument("--table", help="DynamoDB table name (required for dynamo)")
    parser.add_argument("--region", help="DynamoDB region (required for dynamo)")
    parser.add_argument("--json-path", default="storage/job_registry.json", help="Path to JSON registry (for json backend)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a new job entry (fails if exists)")
    create.add_argument("--job-id", required=True)
    create.add_argument("--state", default="RUNNING")
    create.add_argument("--region", required=True)
    create.add_argument("--public-ip", required=True)
    create.add_argument("--pid", required=True)
    create.add_argument("--workload-type", default=None)

    update = subparsers.add_parser("update", help="Update an existing job entry")
    update.add_argument("--job-id", required=True)
    update.add_argument("--state", required=True)
    update.add_argument("--region", default=None)
    update.add_argument("--public-ip", default=None)
    update.add_argument("--pid", default=None)
    update.add_argument("--workload-type", default=None)
    update.add_argument("--expected-version", type=int, default=None, help="Optimistic lock version (Dynamo only)")

    args = parser.parse_args()

    def do_create(reg):
        reg.create(
            args.job_id,
            state=args.state,
            region=args.region,
            public_ip=args.public_ip,
            pid=args.pid,
            workload_type=args.workload_type,
        )
        print(f"Created job {args.job_id}")

    def do_update(reg):
        extra = {}
        if args.region:
            extra["region"] = args.region
        if args.public_ip:
            extra["public_ip"] = args.public_ip
        if args.pid:
            extra["pid"] = args.pid
        if args.workload_type:
            extra["workload_type"] = args.workload_type

        # DynamoRegistry.update signature: (job_id, state, expected_version=None, **attrs)
        reg.update(args.job_id, args.state, expected_version=args.expected_version, **extra)
        print(f"Updated job {args.job_id}")

    if args.backend == "dynamo":
        if not args.table or not args.region:
            raise SystemExit("Dynamo backend requires --table and --region")
        if args.command == "create":
            ensure_dynamo(args.table, args.region, do_create)
        elif args.command == "update":
            ensure_dynamo(args.table, args.region, do_update)
    else:
        if args.command == "create":
            ensure_json(args.json_path, do_create)
        elif args.command == "update":
            ensure_json(args.json_path, do_update)


if __name__ == "__main__":
    main()

