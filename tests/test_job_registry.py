import unittest
import tempfile
import os
import json
from storage.job_registry import JobRegistry

class TestJobRegistry(unittest.TestCase):
    def setUp(self):
        """Create temporary registry file for testing"""
        self.temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')
        initial_data = {
            "job-1": {
                "state": "RUNNING",
                "region": "us-east-1",
                "pid": 1234,
                "public_ip": "1.2.3.4"
            }
        }
        json.dump(initial_data, self.temp_file)
        self.temp_file.close()
        self.registry = JobRegistry(self.temp_file.name)
    
    def tearDown(self):
        """Clean up temporary file"""
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)
    
    def test_get_job(self):
        """Test retrieving a job"""
        job = self.registry.get("job-1")
        self.assertEqual(job["state"], "RUNNING")
        self.assertEqual(job["region"], "us-east-1")
        self.assertEqual(job["pid"], 1234)
        self.assertEqual(job["public_ip"], "1.2.3.4")
    
    def test_update_job_state(self):
        """Test updating job state"""
        self.registry.update("job-1", "CHECKPOINTING")
        job = self.registry.get("job-1")
        self.assertEqual(job["state"], "CHECKPOINTING")
    
    def test_update_job_with_kwargs(self):
        """Test updating job with additional fields"""
        self.registry.update("job-1", "RUNNING", region="us-west-2", public_ip="5.6.7.8")
        job = self.registry.get("job-1")
        self.assertEqual(job["state"], "RUNNING")
        self.assertEqual(job["region"], "us-west-2")
        self.assertEqual(job["public_ip"], "5.6.7.8")
    
    def test_get_nonexistent_job(self):
        """Test getting non-existent job raises error"""
        with self.assertRaises(KeyError):
            self.registry.get("job-nonexistent")

if __name__ == '__main__':
    unittest.main()

