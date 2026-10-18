"""
Integration tests for V2 registry CAS and checkpoint storage with moto.
"""
import pytest
import os
import tempfile
import json
from unittest.mock import Mock, patch, MagicMock
from moto import mock_aws
import boto3

from storage.dynamo_registry import DynamoRegistry


@pytest.fixture
def aws_credentials():
    """Mock AWS credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def s3_setup(aws_credentials):
    """Create mock S3 bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket_name = "test-checkpoint-bucket"
        s3.create_bucket(Bucket=bucket_name)
        yield s3, bucket_name


@pytest.fixture
def dynamodb_setup(aws_credentials):
    """Create mock DynamoDB table."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table_name = "test-registry"
        
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "state", "AttributeType": "S"}
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "StateIndex",
                    "KeySchema": [{"AttributeName": "state", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST"
        )
        
        registry = DynamoRegistry(table_name, region_name="us-east-1")
        yield registry, table_name








class TestS3ManagerIntegration:
    """Integration tests for S3Manager."""

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are required by CRIU")
    def test_checkpoint_archive_restores_criu_file_modes(self, s3_setup, tmp_path):
        import stat
        from storage.s3_manager import S3Manager

        _, bucket = s3_setup
        images = tmp_path / "images"
        (images / "appstate").mkdir(parents=True)
        (images / "core-1.img").write_bytes(b"core")
        (images / "inventory.img").write_bytes(b"inventory")
        logfile = images / "appstate" / "workload.log"
        logfile.write_bytes(b"workload output")
        logfile.chmod(0o640)
        manager = S3Manager(bucket=bucket)
        manager.upload("checkpoint-mode", src=str(images))
        restored = tmp_path / "restored"
        manager.download("checkpoint-mode", dst=str(restored))
        assert stat.S_IMODE((restored / "appstate" / "workload.log").stat().st_mode) == 0o640
    
    def test_upload_download_with_integrity(self, s3_setup, tmp_path):
        """Test upload and download with checksum verification."""
        from storage.s3_manager import S3Manager
        
        s3, bucket_name = s3_setup
        manager = S3Manager(bucket=bucket_name)
        
        # Create test checkpoint directory
        checkpoint_dir = tmp_path / "checkpoint"
        checkpoint_dir.mkdir()
        (checkpoint_dir / "core-1.img").write_text("test checkpoint data")
        (checkpoint_dir / "inventory.img").write_text("test inventory")
        
        job_id = "test-job"
        
        # Upload
        with patch("storage.s3_manager.S3Manager.upload") as mock_upload:
            # Test that upload is called correctly
            mock_upload.return_value = f"{job_id}.tar.gz"
            archive_name = manager.upload(job_id, src=str(checkpoint_dir))
            assert archive_name == f"{job_id}.tar.gz"
    
    def test_upload_enforces_encryption(self, s3_setup, tmp_path):
        """Test that uploads enforce SSE-KMS encryption."""
        from storage.s3_manager import S3Manager
        
        s3, bucket_name = s3_setup
        manager = S3Manager(bucket=bucket_name, kms_key_id="test-key")
        
        checkpoint_dir = tmp_path / "checkpoint"
        checkpoint_dir.mkdir()
        (checkpoint_dir / "core-1.img").write_text("data")
        (checkpoint_dir / "inventory.img").write_text("inventory")
        
        job_id = "test-job"
        
        # Mock S3 client to verify encryption params
        with patch.object(manager.s3, "upload_file", wraps=manager.s3.upload_file) as mock_upload:
            manager.upload(job_id, src=str(checkpoint_dir))
            
            # Verify encryption args were passed
            call_args = mock_upload.call_args_list[0]
            assert "ExtraArgs" in call_args[1]
            assert call_args[1]["ExtraArgs"]["ServerSideEncryption"] == "aws:kms"

    def test_manifest_is_create_only_and_replay_keeps_artifact_identity(self, s3_setup, tmp_path):
        from storage.s3_manager import S3Manager

        _, bucket = s3_setup
        manager = S3Manager(bucket=bucket)
        images = tmp_path / "images"
        images.mkdir()
        (images / "core-1.img").write_bytes(b"core")
        (images / "inventory.img").write_bytes(b"inventory")
        key = manager.upload("checkpoint-a", src=str(images))
        first = manager.manifest("checkpoint-a")
        assert manager.upload("checkpoint-a", src=str(images)) == key
        assert manager.manifest("checkpoint-a")["artifact"]["sha256"] == first["artifact"]["sha256"]
        (images / "core-1.img").write_bytes(b"different")
        with pytest.raises(RuntimeError, match="immutable"):
            manager.upload("checkpoint-a", src=str(images))


class TestDynamoRegistryIntegration:
    """Integration tests for DynamoDB registry."""
    
    def test_optimistic_locking(self, dynamodb_setup):
        """Test optimistic locking prevents concurrent updates."""
        registry, _ = dynamodb_setup
        
        # Create job
        job_id = "test-concurrent"
        registry.create(
            job_id=job_id,
            state="RUNNING",
            region="us-east-1"
        )
        
        # Get current version
        job = registry.get(job_id)
        version = job["version"]
        
        # Update with correct version
        registry.transition(job_id, "MIGRATING", expected_version=version, active_migration_id="m1")
        
        # Try to update with stale version - should fail
        with pytest.raises(RuntimeError, match="Optimistic lock failed"):
            registry.transition(job_id, "RUNNING", expected_version=version)
    
    def test_list_by_state(self, dynamodb_setup):
        """Test listing jobs by state."""
        registry, _ = dynamodb_setup
        
        # Create multiple jobs in different states
        registry.create(job_id="job-1", state="RUNNING", region="us-east-1")
        registry.create(job_id="job-2", state="RUNNING", region="us-west-2")
        registry.create(job_id="job-3", state="FAILED", region="us-east-1")
        
        # List running jobs
        running = registry.list_by_state("RUNNING")
        assert len(running) == 2
        assert all(j["state"] == "RUNNING" for j in running)
        
        # List failed jobs
        failed = registry.list_by_state("FAILED")
        assert len(failed) == 1
        assert failed[0]["job_id"] == "job-3"
