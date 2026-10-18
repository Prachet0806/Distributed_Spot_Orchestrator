# storage/s3_manager.py
import argparse
import boto3
import tarfile
import os
import sys
import json
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from checkpoint.validate_checkpoint import validate as validate_checkpoint
from common_constants import CHECKPOINT_DIR

MANIFEST_SCHEMA_VERSION = "1"
MULTIPART_PART_SIZE_BYTES = 64 * 1024 * 1024  # ADR-016 informational; boto upload_file manages parts


def _safe_members(tar):
    """Yield members after rejecting absolute paths and path traversal (tar-slip)."""
    for member in tar.getmembers():
        name = member.name
        if (os.path.isabs(name) or ".." in name.split("/") or "\\" in name
                or ":" in name or not (member.isfile() or member.isdir())):
            raise RuntimeError(f"Unsafe archive member: {name!r}")
        yield member


class S3Manager:
    def __init__(self, bucket, kms_key_id=None, timeout=300, *, s3_client=None, endpoint_url=None):
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
        self.s3 = s3_client if s3_client is not None else boto3.client("s3", config=config, endpoint_url=endpoint_url)

    def upload(self, job_id, src=CHECKPOINT_DIR):
        self._validate_identity(job_id)
        validate_checkpoint(src)
        for root, dirs, files in os.walk(src):
            for name in dirs + files:
                if os.path.islink(os.path.join(root, name)):
                    raise RuntimeError("checkpoint cannot contain links")
        with tempfile.TemporaryDirectory(prefix="checkpoint-upload-") as tmp:
            archive_path = os.path.join(tmp, "artifact.tar.gz")
            import gzip
            with open(archive_path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w") as tar:
                    tar.add(src, arcname="checkpoint")
            digest = _sha256_file(archive_path)
            archive_name = f"{job_id}.tar.gz"
            extra = {"ServerSideEncryption": "aws:kms", "Metadata": {"sha256": digest}}
            if self.kms_key_id:
                extra["SSEKMSKeyId"] = self.kms_key_id
            try:
                previous = self.s3.head_object(Bucket=self.bucket, Key=archive_name)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code not in {"404", "NoSuchKey", "NotFound"}:
                    raise
                previous = None
            if previous and previous.get("Metadata", {}).get("sha256") != digest:
                raise RuntimeError("checkpoint object identity is immutable")
            if previous is None:
                self.s3.upload_file(archive_path, self.bucket, archive_name, ExtraArgs=extra)
            head = self.s3.head_object(Bucket=self.bucket, Key=archive_name)
            files = {str(path.relative_to(Path(src))).replace("\\", "/"): {"sha256": _sha256_file(path), "size_bytes": path.stat().st_size}
                     for path in Path(src).rglob("*") if path.is_file()}
            manifest = {"files": files, "schema_version": MANIFEST_SCHEMA_VERSION, "job_id": job_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "artifact": {"key": archive_name, "size_bytes": os.path.getsize(archive_path),
                                     "sha256": digest, "version_id": head.get("VersionId")}}
            checksum_path, manifest_path = os.path.join(tmp, "sha256"), os.path.join(tmp, "manifest.json")
            with open(checksum_path, "w") as out:
                out.write(digest)
            with open(manifest_path, "w") as out:
                json.dump(manifest, out, sort_keys=True)
            self.s3.upload_file(checksum_path, self.bucket, f"{job_id}.sha256", ExtraArgs=extra)
            # Publish manifest last: its presence is the commit point.
            manifest_bytes = Path(manifest_path).read_bytes()
            write_args = {"Bucket": self.bucket, "Key": f"{job_id}.manifest.json",
                          "Body": manifest_bytes, "IfNoneMatch": "*",
                          "ServerSideEncryption": "aws:kms",
                          "Metadata": {"sha256": hashlib.sha256(manifest_bytes).hexdigest()}}
            if self.kms_key_id:
                write_args["SSEKMSKeyId"] = self.kms_key_id
            try:
                self.s3.put_object(**write_args)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code not in {"412", "PreconditionFailed"}:
                    raise
                committed = self.manifest(job_id)
                if committed["artifact"]["sha256"] != digest:
                    raise RuntimeError("checkpoint manifest identity conflict") from exc
            return archive_name

    @staticmethod
    def _validate_identity(identity):
        import re
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", identity):
            raise ValueError("unsafe checkpoint identity")

    def manifest(self, identity):
        self._validate_identity(identity)
        doc = json.loads(self.s3.get_object(Bucket=self.bucket, Key=f"{identity}.manifest.json")["Body"].read())
        if doc.get("schema_version") != MANIFEST_SCHEMA_VERSION or doc.get("job_id") != identity:
            raise RuntimeError("Unsupported or mismatched checkpoint manifest schema")
        if doc.get("artifact", {}).get("key") != f"{identity}.tar.gz":
            raise RuntimeError("checkpoint manifest artifact identity mismatch")
        return doc

    def download(self, job_id, dst=CHECKPOINT_DIR, expected_checksum=None):
        import shutil
        from pathlib import Path
        self._validate_identity(job_id)
        manifest = self.manifest(job_id)
        artifact = manifest["artifact"]
        destination = Path(dst).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError("checkpoint destination already exists; use an isolated staging directory")
        with tempfile.TemporaryDirectory(prefix="checkpoint-download-", dir=destination.parent) as tmp:
            archive_path = os.path.join(tmp, "artifact.tar.gz")
            kwargs = {"ExtraArgs": {"VersionId": artifact["version_id"]}} if artifact.get("version_id") else {}
            self.s3.download_file(self.bucket, artifact["key"], archive_path, **kwargs)
            actual = _sha256_file(archive_path)
            if actual != artifact["sha256"] or (expected_checksum and actual != expected_checksum):
                raise RuntimeError("Checkpoint checksum mismatch")
            if os.path.getsize(archive_path) != int(artifact["size_bytes"]):
                raise RuntimeError("Checkpoint manifest size mismatch")
            stage = Path(tmp) / "images"
            stage.mkdir()
            directory_modes = []
            with tarfile.open(archive_path) as tar:
                for member in _safe_members(tar):
                    parts = member.name.split("/")
                    if parts[0] != "checkpoint":
                        raise RuntimeError("archive has an unexpected root")
                    target = stage.joinpath(*parts[1:])
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        directory_modes.append((target, member.mode & 0o777))
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with tar.extractfile(member) as inp, target.open("wb") as out:
                            shutil.copyfileobj(inp, out)
                        target.chmod(member.mode & 0o777)
            for directory, mode in sorted(directory_modes, key=lambda item: len(item[0].parts), reverse=True):
                directory.chmod(mode)
            validate_checkpoint(stage)
            os.replace(stage, destination)
        return manifest

    def cleanup_migration_artifacts(self, migration_id):
        # Durable checkpoints are governed by lineage retention and locks.
        return []

    def delete(self, checkpoint_id):
        self._validate_identity(checkpoint_id)
        for suffix in (".manifest.json", ".sha256", ".tar.gz"):
            self.s3.delete_object(Bucket=self.bucket, Key=checkpoint_id + suffix)


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


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    main()
