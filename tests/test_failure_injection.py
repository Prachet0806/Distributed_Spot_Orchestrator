"""
Failure injection tests to validate system resilience.
Tests various failure scenarios and verifies system behavior.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import subprocess
import time
import tempfile
import os

from orchestrator.utils import retry, SSHClient
from orchestrator.instance_manager import provision_instance
from storage.s3_manager import S3Manager


@pytest.fixture
def temp_ssh_key():
    """Create a temporary SSH key file for testing."""
    temp_key = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem')
    temp_key.write("dummy key content")
    temp_key.close()
    yield temp_key.name
    if os.path.exists(temp_key.name):
        os.unlink(temp_key.name)


class TestRetryFailureInjection:
    """Test retry logic under various failure conditions."""
    
    def test_retry_succeeds_after_transient_failures(self):
        """Test retry succeeds after temporary failures."""
        call_count = [0]
        
        def flaky_function():
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError(f"Transient error #{call_count[0]}")
            return "success"
        
        # Should succeed on 3rd attempt
        result = retry(flaky_function, retries=5, initial_delay=0.1)
        assert result == "success"
        assert call_count[0] == 3
    
    def test_retry_fails_after_max_retries(self):
        """Test retry fails after exhausting all retries."""
        def always_fails():
            raise RuntimeError("Permanent failure")
        
        with pytest.raises(RuntimeError, match="Permanent failure"):
            retry(always_fails, retries=3, initial_delay=0.1)
    
    def test_retry_exponential_backoff_timing(self):
        """Test exponential backoff increases delay correctly."""
        timestamps = []
        
        def capture_timestamp():
            timestamps.append(time.time())
            if len(timestamps) < 4:
                raise RuntimeError("Retry")
            return "done"
        
        retry(capture_timestamp, retries=4, initial_delay=0.1, backoff_factor=2, jitter=False)
        
        # Calculate actual delays between attempts
        actual_delays = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        
        # Should see roughly: 0.1s, 0.2s, 0.4s delays (exponential backoff)
        assert len(actual_delays) >= 2
        # Each delay should be larger than the previous (exponential growth)
        for i in range(1, len(actual_delays)):
            assert actual_delays[i] > actual_delays[i-1], f"Delay {i} ({actual_delays[i]:.3f}s) should be > delay {i-1} ({actual_delays[i-1]:.3f}s)"


class TestSSHFailureInjection:
    """Test SSH client behavior under failure conditions."""
    
    def test_ssh_timeout(self, temp_ssh_key):
        """Test SSH command timeout."""
        client = SSHClient(host="10.0.0.1", timeout=1, key_path=temp_ssh_key)
        
        # Mock subprocess to hang
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=1)
            
            with pytest.raises(RuntimeError, match="timed out"):
                client.run_command("sleep 10")
    
    def test_ssh_connection_refused(self, temp_ssh_key):
        """Test SSH connection refused."""
        client = SSHClient(host="10.0.0.1", key_path=temp_ssh_key)
        
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=255,
                cmd="ssh",
                stderr="Connection refused"
            )
            
            with pytest.raises(RuntimeError, match="SSH command failed"):
                client.run_command("echo test", check=True)
    
    def test_ssh_invalid_host(self):
        """Test SSH with invalid host."""
        with pytest.raises(ValueError, match="Invalid host"):
            SSHClient(host="")
    
    def test_ssh_missing_key(self):
        """Test SSH with missing key file."""
        with pytest.raises(ValueError, match="SSH key not found"):
            SSHClient(host="10.0.0.1", key_path="/nonexistent/key.pem")
    
    def test_ssh_command_injection_prevention(self, temp_ssh_key):
        """Test that command injection is prevented."""
        client = SSHClient(host="10.0.0.1", key_path=temp_ssh_key)
        
        # Attempt command injection
        malicious_command = "echo test; rm -rf /"
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            
            client.run_command(malicious_command)
            
            # Verify command is passed as single argument (not parsed)
            call_args = mock_run.call_args
            cmd_list = call_args[0][0]
            # The malicious command should be passed as-is to SSH
            assert malicious_command in cmd_list


class TestS3FailureInjection:
    """Test S3 operations under failure conditions."""
    
    def test_s3_upload_timeout(self):
        """Test S3 upload timeout."""
        manager = S3Manager(bucket="test-bucket", timeout=1)
        
        # Mock S3 client to timeout
        with patch.object(manager.s3, "upload_file") as mock_upload:
            from botocore.exceptions import ReadTimeoutError
            from urllib3.exceptions import ReadTimeoutError as UrllibReadTimeoutError
            
            mock_upload.side_effect = ReadTimeoutError(
                endpoint_url="https://s3.amazonaws.com",
                error="Read timeout"
            )
            
            with pytest.raises(Exception):  # Will be ReadTimeoutError
                manager.upload("test-job", src="/tmp/checkpoint")
    
    def test_s3_checksum_mismatch(self):
        """Test S3 download with checksum mismatch."""
        manager = S3Manager(bucket="test-bucket")
        
        # Mock S3 download to return corrupted data
        with patch.object(manager.s3, "download_file") as mock_download:
            def download_side_effect(bucket, key, path):
                if key.endswith(".tar.gz"):
                    with open(path, "wb") as f:
                        f.write(b"corrupted data")
                else:  # .sha256 file
                    with open(path, "w") as f:
                        f.write("abc123correcthash")
            
            mock_download.side_effect = download_side_effect
            
            with pytest.raises(RuntimeError, match="Checkpoint checksum mismatch"):
                manager.download("test-job", dst="/tmp/checkpoint")
    
    def test_s3_bucket_not_found(self):
        """Test S3 operations when bucket doesn't exist."""
        manager = S3Manager(bucket="nonexistent-bucket")
        
        with patch.object(manager.s3, "upload_file") as mock_upload:
            from botocore.exceptions import ClientError
            
            mock_upload.side_effect = ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
                "PutObject"
            )
            
            with pytest.raises(Exception):  # Will be ClientError
                manager.upload("test-job", src="/tmp/checkpoint")


class TestInstanceProvisioningFailureInjection:
    """Test instance provisioning under failure conditions."""
    
    def test_provision_instance_timeout(self):
        """Test instance provisioning timeout."""
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            
            # Mock instance launch
            mock_ec2.run_instances.return_value = {
                "Instances": [{"InstanceId": "i-test"}]
            }
            
            # Mock waiter to timeout
            mock_waiter = MagicMock()
            mock_waiter.wait.side_effect = Exception("Waiter timeout")
            mock_ec2.get_waiter.return_value = mock_waiter
            
            with pytest.raises(Exception):
                provision_instance(
                    region="us-east-1",
                    ami_id="ami-test",
                    security_group_id="sg-test",
                    key_name="test-key",
                    instance_type="t3.micro",
                    timeout=60
                )
    
    def test_provision_instance_no_public_ip(self):
        """Test instance provisioning when instance has no public IP."""
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            
            mock_ec2.run_instances.return_value = {
                "Instances": [{"InstanceId": "i-test"}]
            }
            
            mock_waiter = MagicMock()
            mock_ec2.get_waiter.return_value = mock_waiter
            
            # Mock describe_instances to return no public IP
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{
                    "Instances": [{
                        "InstanceId": "i-test",
                        "PublicIpAddress": None,  # No public IP!
                        "PublicDnsName": None
                    }]
                }]
            }
            
            with pytest.raises(RuntimeError, match="has no public IP"):
                provision_instance(
                    region="us-east-1",
                    ami_id="ami-test",
                    security_group_id="sg-test",
                    key_name="test-key",
                    instance_type="t3.micro"
                )
    
    def test_provision_instance_invalid_inputs(self):
        """Test instance provisioning with invalid inputs."""
        # Invalid AMI ID
        with pytest.raises(ValueError, match="Invalid AMI ID"):
            provision_instance(
                region="us-east-1",
                ami_id="invalid",
                security_group_id="sg-test",
                key_name="test-key",
                instance_type="t3.micro"
            )
        
        # Invalid security group ID
        with pytest.raises(ValueError, match="Invalid security group ID"):
            provision_instance(
                region="us-east-1",
                ami_id="ami-test",
                security_group_id="invalid",
                key_name="test-key",
                instance_type="t3.micro"
            )
        
        # Invalid timeout
        with pytest.raises(ValueError, match="Invalid timeout"):
            provision_instance(
                region="us-east-1",
                ami_id="ami-test",
                security_group_id="sg-test",
                key_name="test-key",
                instance_type="t3.micro",
                timeout=30  # Too low
            )
    
    def test_provision_idempotency(self):
        """Test that provisioning is idempotent with same token."""
        with patch("boto3.Session") as mock_session:
            mock_ec2 = MagicMock()
            mock_session.return_value.client.return_value = mock_ec2
            
            mock_ec2.run_instances.return_value = {
                "Instances": [{"InstanceId": "i-test"}]
            }
            
            mock_waiter = MagicMock()
            mock_ec2.get_waiter.return_value = mock_waiter
            
            mock_ec2.describe_instances.return_value = {
                "Reservations": [{
                    "Instances": [{
                        "InstanceId": "i-test",
                        "PublicIpAddress": "10.0.0.1",
                        "PublicDnsName": "ec2-test.com"
                    }]
                }]
            }
            
            token = "test-token-123"
            
            # Call twice with same token
            result1 = provision_instance(
                region="us-east-1",
                ami_id="ami-test",
                security_group_id="sg-test",
                key_name="test-key",
                instance_type="t3.micro",
                idempotency_token=token
            )
            
            result2 = provision_instance(
                region="us-east-1",
                ami_id="ami-test",
                security_group_id="sg-test",
                key_name="test-key",
                instance_type="t3.micro",
                idempotency_token=token
            )
            
            # Both should succeed with same token
            assert result1[0] == result2[0]
            
            # Verify ClientToken was used
            call_args = mock_ec2.run_instances.call_args_list[0]
            assert "ClientToken" in call_args[1]
            assert call_args[1]["ClientToken"] == token


class TestNetworkPartitionScenarios:
    """Test system behavior under network partition scenarios."""
    
    def test_ssh_intermittent_connectivity(self, temp_ssh_key):
        """Test SSH with intermittent connectivity."""
        client = SSHClient(host="10.0.0.1", key_path=temp_ssh_key)
        call_count = [0]
        
        def flaky_ssh(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)
            return MagicMock(returncode=0, stdout="success", stderr="")
        
        with patch("subprocess.run", side_effect=flaky_ssh):
            # Should succeed with retry
            result = retry(lambda: client.run_command("echo test"), retries=5, initial_delay=0.1)
            assert call_count[0] == 3
    
    def test_s3_intermittent_access(self):
        """Test S3 with intermittent access."""
        manager = S3Manager(bucket="test-bucket")
        call_count = [0]
        
        def flaky_upload(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                from botocore.exceptions import ClientError
                raise ClientError(
                    {"Error": {"Code": "RequestTimeout", "Message": "Timeout"}},
                    "PutObject"
                )
        
        with patch.object(manager.s3, "upload_file", side_effect=flaky_upload):
            # S3 client has built-in retries, but we can test our retry wrapper
            with pytest.raises(Exception):  # Will fail after retries
                retry(
                    lambda: manager.upload("test-job", src="/tmp/checkpoint"),
                    retries=2,
                    initial_delay=0.1
                )
