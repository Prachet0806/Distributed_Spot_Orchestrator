import unittest
import tempfile
import os
from unittest.mock import patch, MagicMock
from orchestrator.utils import SSHClient

class TestSSHClient(unittest.TestCase):
    def setUp(self):
        """Create a temporary SSH key file for testing"""
        self.temp_key = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem')
        self.temp_key.write("dummy key content")
        self.temp_key.close()
        self.key_path = self.temp_key.name
    
    def tearDown(self):
        """Clean up temporary key file"""
        if os.path.exists(self.key_path):
            os.unlink(self.key_path)
    
    def test_ssh_client_initialization(self):
        """Test SSHClient initialization"""
        client = SSHClient(host="1.2.3.4", user="ubuntu", key_path=self.key_path)
        self.assertEqual(client.host, "1.2.3.4")
        self.assertEqual(client.user, "ubuntu")
        self.assertEqual(client.port, 22)
        self.assertFalse(client.connected)
    
    def test_ssh_client_with_custom_port(self):
        """Test SSHClient with custom port"""
        client = SSHClient(host="1.2.3.4", port=2222, key_path=self.key_path)
        self.assertEqual(client.port, 2222)
    
    @patch('subprocess.run')
    def test_ssh_command_execution_success(self, mock_run):
        """Test successful SSH command execution"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Hello from remote\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        client = SSHClient(host="1.2.3.4", key_path=self.key_path)
        result = client.run_command("echo 'test'")
        
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Hello from remote\n")
        mock_run.assert_called_once()
    
    @patch('subprocess.run')
    def test_ssh_command_execution_failure(self, mock_run):
        """Test SSH command execution failure"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Command failed"
        mock_run.return_value = mock_result
        
        client = SSHClient(host="1.2.3.4", key_path=self.key_path)
        
        with self.assertRaises(RuntimeError):
            client.run_command("invalid_command", check=True)
    
    @patch('subprocess.run')
    def test_ssh_connect(self, mock_run):
        """Test SSH connection"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "SSH connection test\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        client = SSHClient(host="1.2.3.4", key_path=self.key_path)
        client.connect()
        
        self.assertTrue(client.connected)
    
    def test_ssh_close(self):
        """Test SSH connection close"""
        client = SSHClient(host="1.2.3.4", key_path=self.key_path)
        client.connected = True
        client.close()
        self.assertFalse(client.connected)

if __name__ == '__main__':
    unittest.main()

