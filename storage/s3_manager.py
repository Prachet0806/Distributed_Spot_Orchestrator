# storage/s3_manager.py
import argparse
import boto3
import tarfile
import os
import sys


class S3Manager:
    def __init__(self, bucket):
        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def upload(self, job_id, src="/opt/job_workspace/checkpoint"):
        archive_name = f"{job_id}.tar.gz"
        archive_path = os.path.join("/tmp", archive_name)

        print(f"📦 Compressing {src} to {archive_path}...")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(src, arcname=os.path.basename(src))

        print(f"⬆️  Uploading to s3://{self.bucket}/{archive_name}...")
        self.s3.upload_file(archive_path, self.bucket, archive_name)
        return archive_name

    def download(self, job_id, dst="/opt/job_workspace/checkpoint"):
        archive_name = f"{job_id}.tar.gz"
        archive_path = os.path.join("/tmp", archive_name)

        print(f"⬇️  Downloading s3://{self.bucket}/{archive_name}...")
        self.s3.download_file(self.bucket, archive_name, archive_path)

        print(f"📂 Extracting to {dst}...")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with tarfile.open(archive_path) as tar:
            tar.extractall(path=os.path.dirname(dst))


def main():
    parser = argparse.ArgumentParser(description="Worker S3 Checkpoint Manager")
    parser.add_argument("action", choices=["upload", "download"], help="Action to perform")
    parser.add_argument("job_id", help="Unique Job ID")
    parser.add_argument("--bucket", required=True, help="S3 Bucket Name")

    args = parser.parse_args()

    manager = S3Manager(bucket=args.bucket)

    try:
        if args.action == "upload":
            manager.upload(args.job_id)
        elif args.action == "download":
            manager.download(args.job_id)
        print("✅ Operation successful")
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
