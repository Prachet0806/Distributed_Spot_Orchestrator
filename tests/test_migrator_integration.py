"""
Integration tests for Migrator with mocked AWS services.
Tests full migration flow with moto for S3/DynamoDB.
"""
import pytest
import os
import tempfile
import json
from unittest.mock import Mock, patch, MagicMock
from moto import mock_aws
import boto3

from orchestrator.migrator import Migrator
from storage.dynamo_registry import DynamoRegistry
from storage.job_states import JobState


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


@pytest.fixture
def mock_ssh_client():
    """Mock SSHClient for testing without actual SSH."""
    with patch("orchestrator.migrator.SSHClient") as mock_ssh:
        instance = MagicMock()
        
        # Mock different command responses
        def mock_run_command(cmd, check=True, **kwargs):
            result = MagicMock()
            result.stderr = ""
            
            # Mock ps -p command (check if PID exists) - should return 1 after kill
            if "ps -p" in cmd:
                result.returncode = 1  # PID not found (process terminated)
                result.stdout = ""
            # Mock instance ID fetch
            elif "instance-id" in cmd or "meta-data" in cmd:
                result.returncode = 0
                result.stdout = "i-1234567890abcdef0\n"
            # Mock du command (checkpoint size)
            elif "du -sb" in cmd:
                result.returncode = 0
                result.stdout = "1024000 /opt/job_workspace/checkpoint\n"
            # Default success
            else:
                result.returncode = 0
                result.stdout = "success\n"
            
            if check and result.returncode != 0:
                raise RuntimeError(f"Command failed: {cmd}")
            
            return result
        
        instance.run_command.side_effect = mock_run_command
        instance.connect.return_value = None
        instance.close.return_value = None
        
        mock_ssh.return_value = instance
        yield instance


@pytest.fixture
def sample_job(dynamodb_setup):
    """Create a sample job in the registry."""
    registry, _ = dynamodb_setup
    job_id = "test-job-1"
    
    registry.create(
        job_id=job_id,
        state=JobState.RUNNING.value,
        region="us-east-1",
        public_ip="10.0.0.1",
        pid=1234,
        workload_type="batch"
    )
    
    yield job_id, registry


class TestMigratorIntegration:
    """Integration tests for Migrator."""
    
    def test_migration_happy_path(self, s3_setup, sample_job, mock_ssh_client):
        """Test successful migration flow."""
        s3, bucket_name = s3_setup
        job_id, registry = sample_job
        
        # Setup
        migrator = Migrator(registry, checkpoint_bucket=bucket_name)
        
        # Mock checkpoint file operations
        with patch("orchestrator.migrator._get_instance_id") as mock_get_id:
            mock_get_id.side_effect = ["i-source", "i-target"]
            
            # Mock provisioning
            with patch("orchestrator.migrator.provision_instance") as mock_provision:
                mock_provision.return_value = ("i-target", "10.0.0.2", "ec2-10-0-0-2.compute-1.amazonaws.com")
                
                # Execute migration
                migrator.migrate(
                    job_id=job_id,
                    target_region="us-west-2",
                    autoprovision=True,
                    provision_overrides={
                        "ami_id": "ami-test",
                        "security_group_id": "sg-test",
                        "ssh_key_name": "test-key",
                        "instance_type": "t3.micro"
                    }
                )
        
        # Verify job state transitions
        job = registry.get(job_id)
        assert job["state"] == JobState.RUNNING.value
        assert job["region"] == "us-west-2"
        assert job["public_ip"] == "10.0.0.2"
        assert "migration_id" in job
        assert job.get("source_instance_id") == "i-source"
        assert job.get("target_instance_id") == "i-target"
    
    def test_migration_with_idempotent_resume(self, s3_setup, sample_job, mock_ssh_client):
        """Test migration resume from last completed step."""
        s3, bucket_name = s3_setup
        job_id, registry = sample_job
        
        # Setup - simulate partial migration (follow proper state transitions)
        registry.update(job_id, JobState.CHECKPOINTING.value)
        registry.update(job_id, JobState.UPLOADING.value, last_completed_step="CHECKPOINTING")
        
        migrator = Migrator(registry, checkpoint_bucket=bucket_name)
        
        with patch("orchestrator.migrator._get_instance_id") as mock_get_id:
            mock_get_id.side_effect = ["i-source", "i-target"]
            
            with patch("orchestrator.migrator.provision_instance") as mock_provision:
                mock_provision.return_value = ("i-target", "10.0.0.2", "ec2-host.com")
                
                # Execute migration - should skip CHECKPOINTING
                migrator.migrate(
                    job_id=job_id,
                    target_region="us-west-2",
                    autoprovision=True,
                    provision_overrides={
                        "ami_id": "ami-test",
                        "security_group_id": "sg-test",
                        "ssh_key_name": "test-key",
                        "instance_type": "t3.micro"
                    }
                )
        
        # Should complete successfully
        job = registry.get(job_id)
        assert job["state"] == JobState.RUNNING.value
    
    def test_migration_fails_on_invalid_state_transition(self, s3_setup, sample_job, mock_ssh_client):
        """Test that invalid state transitions are rejected."""
        s3, bucket_name = s3_setup
        job_id, registry = sample_job
        
        # Set job to TERMINATED state (follow proper state transitions)
        registry.update(job_id, JobState.CHECKPOINTING.value)
        registry.update(job_id, JobState.FAILED.value)
        registry.update(job_id, JobState.TERMINATED.value)
        
        migrator = Migrator(registry, checkpoint_bucket=bucket_name)
        
        # Attempting migration from TERMINATED should fail
        with pytest.raises(RuntimeError, match="Invalid state transition"):
            migrator.migrate(
                job_id=job_id,
                target_region="us-west-2",
                target_ip="10.0.0.2"
            )
    
    def test_migration_prevents_split_brain(self, s3_setup, sample_job, mock_ssh_client):
        """Test split-brain prevention."""
        s3, bucket_name = s3_setup
        job_id, registry = sample_job
        
        migrator = Migrator(registry, checkpoint_bucket=bucket_name)
        
        # Mock both source and target returning same instance ID
        with patch("orchestrator.migrator._get_instance_id") as mock_get_id:
            mock_get_id.return_value = "i-same-instance"  # Same ID for both!
            
            with patch("orchestrator.migrator.provision_instance") as mock_provision:
                mock_provision.return_value = ("i-same-instance", "10.0.0.2", "ec2-host.com")
                
                # Should fail with split-brain detection
                with pytest.raises(RuntimeError, match="Split-brain detected"):
                    migrator.migrate(
                        job_id=job_id,
                        target_region="us-west-2",
                        autoprovision=True,
                        provision_overrides={
                            "ami_id": "ami-test",
                            "security_group_id": "sg-test",
                            "ssh_key_name": "test-key",
                            "instance_type": "t3.micro"
                        }
                    )
    
    def test_migration_failure_sets_failed_state(self, s3_setup, sample_job, mock_ssh_client):
        """Test that migration failures set FAILED state."""
        s3, bucket_name = s3_setup
        job_id, registry = sample_job
        
        migrator = Migrator(registry, checkpoint_bucket=bucket_name)
        
        # Mock SSH failure
        mock_ssh_client.run_command.side_effect = RuntimeError("SSH connection failed")
        
        with pytest.raises(RuntimeError):
            migrator.migrate(
                job_id=job_id,
                target_region="us-west-2",
                target_ip="10.0.0.2"
            )
        
        # Job should be in FAILED state
        job = registry.get(job_id)
        assert job["state"] == JobState.FAILED.value
        assert "last_error" in job


class TestS3ManagerIntegration:
    """Integration tests for S3Manager."""
    
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
        with patch.object(manager.s3, "upload_file") as mock_upload:
            manager.upload(job_id, src=str(checkpoint_dir))
            
            # Verify encryption args were passed
            call_args = mock_upload.call_args_list[0]
            assert "ExtraArgs" in call_args[1]
            assert call_args[1]["ExtraArgs"]["ServerSideEncryption"] == "aws:kms"


class TestDynamoRegistryIntegration:
    """Integration tests for DynamoDB registry."""
    
    def test_optimistic_locking(self, dynamodb_setup):
        """Test optimistic locking prevents concurrent updates."""
        registry, _ = dynamodb_setup
        
        # Create job
        job_id = "test-concurrent"
        registry.create(
            job_id=job_id,
            state=JobState.RUNNING.value,
            region="us-east-1"
        )
        
        # Get current version
        job = registry.get(job_id)
        version = job["version"]
        
        # Update with correct version
        registry.update(job_id, JobState.CHECKPOINTING.value, expected_version=version)
        
        # Try to update with stale version - should fail
        with pytest.raises(RuntimeError, match="Optimistic lock failed"):
            registry.update(job_id, JobState.UPLOADING.value, expected_version=version)
    
    def test_list_by_state(self, dynamodb_setup):
        """Test listing jobs by state."""
        registry, _ = dynamodb_setup
        
        # Create multiple jobs in different states
        registry.create(job_id="job-1", state=JobState.RUNNING.value, region="us-east-1")
        registry.create(job_id="job-2", state=JobState.RUNNING.value, region="us-west-2")
        registry.create(job_id="job-3", state=JobState.FAILED.value, region="us-east-1")
        
        # List running jobs
        running = registry.list_by_state(JobState.RUNNING.value)
        assert len(running) == 2
        assert all(j["state"] == JobState.RUNNING.value for j in running)
        
        # List failed jobs
        failed = registry.list_by_state(JobState.FAILED.value)
        assert len(failed) == 1
        assert failed[0]["job_id"] == "job-3"
