# orchestrator/utils.py
import time
import logging
import subprocess
import os
from typing import Optional

logging.basicConfig(level=logging.INFO)

def retry(fn, retries=3, delay=2):
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i == retries - 1:
                raise
            logging.warning(f"Retry {i+1}/{retries} failed: {e}")
            time.sleep(delay)


class SSHClient:
    """
    SSH client for remote command execution on EC2 instances.
    Uses subprocess with ssh command for simplicity (no extra dependencies).
    """
    
    def __init__(
        self,
        host: str,
        user: str = "ubuntu",
        key_path: Optional[str] = None,
        port: int = 22,
        timeout: int = 30
    ):
        """
        Initialize SSH client.
        
        Args:
            host: IP address or hostname of remote host
            user: SSH username (default: ubuntu)
            key_path: Path to SSH private key (default: ~/.ssh/id_rsa)
            port: SSH port (default: 22)
            timeout: Connection timeout in seconds (default: 30)
        """
        self.host = host
        self.user = user
        self.port = port
        self.timeout = timeout
        
        # Determine SSH key path
        if key_path:
            self.key_path = key_path
        else:
            # Try common SSH key locations
            default_keys = [
                os.path.expanduser("~/.ssh/id_rsa"),
                os.path.expanduser("~/.ssh/id_ed25519"),
                os.path.expanduser("~/.ssh/id_ecdsa"),
            ]
            self.key_path = None
            for key in default_keys:
                if os.path.exists(key):
                    self.key_path = key
                    break
            
            if not self.key_path:
                logging.warning(
                    "No SSH key found. SSH will use default authentication methods."
                )
        
        self.connected = False
    
    def connect(self):
        """
        Test SSH connectivity (connection is established on first command).
        """
        try:
            # Test connection with a simple command
            self.run_command("echo 'SSH connection test'", check=False)
            self.connected = True
            logging.info(f"SSH connection established to {self.user}@{self.host}")
        except Exception as e:
            logging.error(f"Failed to establish SSH connection: {e}")
            raise
    
    def run_command(
        self,
        command: str,
        check: bool = True,
        capture_output: bool = True
    ) -> subprocess.CompletedProcess:
        """
        Execute a remote command via SSH.
        
        Args:
            command: Command to execute on remote host
            check: If True, raise exception on non-zero exit code
            capture_output: If True, capture stdout/stderr
            
        Returns:
            CompletedProcess object with stdout, stderr, returncode
        """
        # Build SSH command
        ssh_cmd = ["ssh"]
        
        # Add SSH options
        ssh_options = [
            "-o", "StrictHostKeyChecking=no",  # Accept new host keys
            "-o", "UserKnownHostsFile=/dev/null",  # Don't save host keys
            "-o", "ConnectTimeout=10",  # Connection timeout
            "-o", "BatchMode=yes",  # Disable password prompts
            "-o", "LogLevel=ERROR",  # Reduce verbosity
        ]
        
        # Add SSH key if specified
        if self.key_path:
            ssh_options.extend(["-i", self.key_path])
        
        # Add port
        ssh_options.extend(["-p", str(self.port)])
        
        # Build full command: ssh [options] user@host "command"
        ssh_cmd.extend(ssh_options)
        ssh_cmd.append(f"{self.user}@{self.host}")
        ssh_cmd.append(command)
        
        logging.debug(f"Executing SSH command: {' '.join(ssh_cmd)}")
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=capture_output,
                text=True,
                timeout=self.timeout,
                check=check
            )
            
            if result.returncode != 0 and check:
                error_msg = result.stderr if result.stderr else "Unknown error"
                raise RuntimeError(
                    f"SSH command failed (exit code {result.returncode}): {error_msg}"
                )
            
            if capture_output and result.stdout:
                logging.debug(f"Command output: {result.stdout[:200]}")  # Log first 200 chars
            
            return result
            
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH command timed out after {self.timeout} seconds")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"SSH command failed: {e.stderr if e.stderr else str(e)}")
        except FileNotFoundError:
            raise RuntimeError(
                "SSH command not found. Ensure OpenSSH client is installed."
            )
    
    def close(self):
        """
        Close SSH connection (no-op for subprocess-based implementation).
        Connection is closed automatically after each command.
        """
        if self.connected:
            logging.debug(f"SSH connection closed to {self.user}@{self.host}")
            self.connected = False
