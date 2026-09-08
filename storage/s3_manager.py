# storage/s3_manager.py
import argparse
import boto3
import tarfile
import os
import sys
import json
import hashlib
import tempfile
from datetime import datetime, timezone
from checkpoint.validate_checkpoint import validate as validate_checkpoint
from common_constants import CHECKPOINT_DIR

MANIFEST_SCHEMA_VERSION = "1"
MULTIPART_PART_SIZE_BYTES = 64 * 1024 * 1024  # ADR-016 informational; boto upload_file manages parts


def _safe_members(tar):
    """Yield members after rejecting absolute paths and path traversal (tar-slip)."""
    for member in tar.getmembers():
        name = member.name
        if os.path.isabs(name) or ".." in name.split("/"):
            raise RuntimeError(f"Unsafe archive member: {name!r}")
        yield member


class S3Manager:
    def __init__(self, bucket, kms_key_id=None, timeout=300):
        """
        Initialize S3 manager with encryption and timeout configuration.
        
        Args:
            bucket: S3 bucket name
            kms_key_id: KMS key ID for SSE-KMS encryption (optional, uses default if None)
            timeout: Timeout for S3 operations in seconds (default: 300)
        """
        if not bucket or not isinstance(bucket, str):
            raise ValueError(f"Invalid bucket name: {bucket}")
        
        self.bucket = bucket.strip()
        self.kms_key_id = kms_key_id
        self.timeout = timeout
        
        # Configure S3 client with timeout
        config = boto3.session.Config(
            connect_timeout=30,
            read_timeout=self.timeout,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
        self.s3 = boto3.client("s3", config=config)

    def upload(self, job_id, src=CHECKPOINT_DIR):
        archive_name = f"{job_id}.tar.gz"
        archive_path = os.path.join(tempfile.gettempdir(), archive_name)
        checksum_name = f"{job_id}.sha256"
        checksum_path = os.path.join(tempfile.gettempdir(), checksum_name)
        manifest_name = f"{job_id}.manifest.json"
        manifest_path = os.path.join(tempfile.gettempdir(), manifest_name)

        print(f"📦 Compressing {src} to {archive_path}...")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(src, arcname=os.path.basename(src))

        digest = _sha256_file(archive_path)
        with open(checksum_path, "w") as f:
            f.write(digest)

        size_bytes = os.path.getsize(archive_path)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifact": {
                "key": archive_name,
                "size_bytes": size_bytes,
                "sha256": digest,
            },
            "source": {"checkpoint_dir": src},
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)

        print(f"⬆️  Uploading to s3://{self.bucket}/{archive_name}...")
        
        # Prepare encryption configuration
        extra_args = {
            'ServerSideEncryption': 'aws:kms'
        }
        if self.kms_key_id:
            extra_args['SSEKMSKeyId'] = self.kms_key_id
        
        # Upload with SSE-KMS encryption enforced
        try:
            self.s3.upload_file(archive_path, self.bucket, archive_name, ExtraArgs=extra_args)
            self.s3.upload_file(checksum_path, self.bucket, checksum_name, ExtraArgs=extra_args)
            self.s3.upload_file(manifest_path, self.bucket, manifest_name, ExtraArgs=extra_args)
        finally:
            # Clean up temp files regardless of success/failure
            for p in (archive_path, checksum_path, manifest_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
        
        print(f"✅ Uploaded with SSE-KMS encryption")
        return archive_name

    def download(self, job_id, dst=CHECKPOINT_DIR):
        archive_name = f"{job_id}.tar.gz"
        archive_path = os.path.join(tempfile.gettempdir(), archive_name)
        checksum_name = f"{job_id}.sha256"
        checksum_path = os.path.join(tempfile.gettempdir(), checksum_name)
        manifest_name = f"{job_id}.manifest.json"
        manifest_path = os.path.join(tempfile.gettempdir(), manifest_name)

        print(f"⬇️  Downloading s3://{self.bucket}/{archive_name}...")
        self.s3.download_file(self.bucket, archive_name, archive_path)
        self.s3.download_file(self.bucket, checksum_name, checksum_path)
        try:
            self.s3.download_file(self.bucket, manifest_name, manifest_path)
        except Exception:
            pass  # manifests predate this change; checksum remains authoritative

        expected = None
        with open(checksum_path) as f:
            expected = f.read().strip()
        actual = _sha256_file(archive_path)
        if expected != actual:
            raise RuntimeError("Checkpoint checksum mismatch")

        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                m_artifact = manifest.get("artifact", {})
                if m_artifact.get("sha256") and m_artifact["sha256"] != actual:
                    raise RuntimeError("Checkpoint manifest checksum mismatch")
                if m_artifact.get("size_bytes") is not None and int(m_artifact["size_bytes"]) != os.path.getsize(archive_path):
                    raise RuntimeError("Checkpoint manifest size mismatch")
            finally:
                try:
                    os.remove(manifest_path)
                except OSError:
                    pass

        print(f"📂 Extracting to {dst}...")
        extract_dir = os.path.dirname(dst) or "."
        os.makedirs(dst, exist_ok=True)
        with tarfile.open(archive_path) as tar:
            tar.extractall(path=extract_dir, members=_safe_members(tar))
        try:
            validate_checkpoint(dst)
        except RuntimeError:
            # Clean up only the extracted checkpoint dir — never the workspace.
            import shutil
            shutil.rmtree(dst, ignore_errors=True)
            raise

        # Clean up temp files
        try:
            os.remove(archive_path)
            os.remove(checksum_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Worker S3 Checkpoint Manager")
    parser.add_argument("action", choices=["upload", "download"], help="Action to perform")
    parser.add_argument("job_id", help="Unique Job ID")
    parser.add_argument("--bucket", required=True, help="S3 Bucket Name")
    parser.add_argument("--kms-key-id", help="KMS key ID for encryption (optional)")
    parser.add_argument("--timeout", type=int, default=300, help="Operation timeout in seconds")

    args = parser.parse_args()

    manager = S3Manager(bucket=args.bucket, kms_key_id=args.kms_key_id, timeout=args.timeout)

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


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
