# orchestrator/utils.py
import time
import logging
import subprocess
import os
from typing import Optional, Callable, Any

logging.basicConfig(level=logging.INFO)

def retry(
    fn: Callable[[], Any],
    retries: int = 3,
    initial_delay: float = 2,
    max_delay: float = 60,
    backoff_factor: float = 2,
    jitter: bool = True
) -> Any:
    """
    Retry a function with exponential backoff.
    
    Args:
        fn: Function to retry
        retries: Maximum number of retries
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_factor: Exponential backoff multiplier
        jitter: Add random jitter to prevent thundering herd
        
    Returns:
        Result of fn()
        
    Raises:
        Last exception if all retries fail
    """
    import random
    
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            if i == retries - 1:
                logging.error(f"All {retries} retries failed: {e}")
                raise
            
            # Calculate delay with exponential backoff
            delay = min(initial_delay * (backoff_factor ** i), max_delay)
            
            # Add jitter (±25% randomization)
            if jitter:
                jitter_range = delay * 0.25
                delay = delay + random.uniform(-jitter_range, jitter_range)
            
            logging.warning(
                f"Retry {i+1}/{retries} failed: {e}. Retrying in {delay:.1f}s"
            )
            time.sleep(delay)


class SSHClient:
    """
    SSH client for remote command execution on EC2 instances.
    Uses subprocess with ssh command for simplicity (no extra dependencies).
    
    Security:
    - Enforces host key verification
    - Requires explicit key path
    - Validates inputs
    """
    
    def __init__(
        self,
        host: str,
        user: str = "ubuntu",
        key_path: Optional[str] = None,
        port: int = 22,
        timeout: int = 30,
        known_hosts_path: Optional[str] = None,
        strict: bool = False,
    ):
        """
        Initialize SSH client.
        
        Args:
            host: IP address or hostname of remote host
            user: SSH username (default: ubuntu)
            key_path: Path to SSH private key (default: ~/.ssh/id_rsa)
            port: SSH port (default: 22)
            timeout: Connection timeout in seconds (default: 30)
            known_hosts_path: Path to known_hosts file (default: ~/.ssh/known_hosts)
            strict: Track D2 — require a pre-pinned known_hosts file and
                refuse first-connect TOFU instead of accept-new.
        
        Raises:
            ValueError: If host is invalid or key path doesn't exist
        """
        # Input validation
        if not host or not isinstance(host, str):
            raise ValueError(f"Invalid host: {host}")
        if not user or not isinstance(user, str):
            raise ValueError(f"Invalid user: {user}")
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid port: {port}")
        if timeout < 1:
            raise ValueError(f"Invalid timeout: {timeout}")
        
        self.host = host.strip()
        self.user = user.strip()
        self.port = port
        self.timeout = timeout
        
        # Determine SSH key path
        if key_path:
            self.key_path = os.path.expanduser(key_path)
            if not os.path.exists(self.key_path):
                raise ValueError(f"SSH key not found: {self.key_path}")
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
                raise ValueError(
                    "No SSH key found. Please specify key_path or create a key in ~/.ssh/"
                )
        
        # Known hosts for host key verification
        self.known_hosts_path = known_hosts_path or os.path.expanduser("~/.ssh/known_hosts")
        self.strict = strict
        
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
        capture_output: bool = True,
        timeout_override: Optional[int] = None
    ) -> subprocess.CompletedProcess:
        """
        Execute a remote command via SSH.
        
        Args:
            command: Command to execute on remote host
            check: If True, raise exception on non-zero exit code
            capture_output: If True, capture stdout/stderr
            timeout_override: Override default timeout for this command
            
        Returns:
            CompletedProcess object with stdout, stderr, returncode
            
        Raises:
            ValueError: If command is empty or invalid
            RuntimeError: If SSH fails
        """
        if not command or not isinstance(command, str):
            raise ValueError(f"Invalid command: {command}")
        
        # Build SSH command
        ssh_cmd = ["ssh"]
        
        # Add SSH options - SECURE configuration
        ssh_options = [
            "-o", "ConnectTimeout=10",  # Connection timeout
            "-o", "ServerAliveInterval=15",  # Keep connection alive
            "-o", "ServerAliveCountMax=3",  # Max missed keepalives
            "-o", "BatchMode=yes",  # Disable password prompts
            "-o", "LogLevel=ERROR",  # Reduce verbosity
        ]
        
        # Host key verification - enforce known_hosts check
        if os.path.exists(self.known_hosts_path):
            ssh_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts_path}"
            ])
        elif self.strict:
            # Track D2: pinned deployments rule on first-connect TOFU.
            raise RuntimeError(
                f"Pinned known_hosts file not found: {self.known_hosts_path}. "
                "Refusing first-connect TOFU (strict mode); pin keys first."
            )
        else:
            # Warn but allow first connection (will add to known_hosts)
            logging.warning(
                f"Known hosts file not found: {self.known_hosts_path}. "
                "First connection will accept host key."
            )
            ssh_options.extend([
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts_path}"
            ])
        
        # Add SSH key (required)
        ssh_options.extend(["-i", self.key_path])
        
        # Add port
        ssh_options.extend(["-p", str(self.port)])
        
        # Build full command: ssh [options] user@host "command"
        ssh_cmd.extend(ssh_options)
        ssh_cmd.append(f"{self.user}@{self.host}")
        ssh_cmd.append(command)
        
        logging.debug(f"Executing SSH command to {self.user}@{self.host}")
        
        timeout = timeout_override if timeout_override is not None else self.timeout
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
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
            raise RuntimeError(f"SSH command timed out after {timeout} seconds")
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
