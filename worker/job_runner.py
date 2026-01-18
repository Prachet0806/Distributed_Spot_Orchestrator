# worker/job_runner.py
import os
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path (works on remote worker)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.jobs.monte_carlo import run

def main():
    pid = os.getpid()
    print(f"Job started with PID {pid}")
    run()

if __name__ == "__main__":
    main()
