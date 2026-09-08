# worker/job_runner.py
import os
import sys
import threading
from pathlib import Path

# Ensure project root is on sys.path (works on remote worker)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker.jobs.monte_carlo import run
from worker.spot_interrupt import monitor_spot_interruption
from worker.constants import SPOT_INTERRUPT_FLAG

FLAG_PATH = SPOT_INTERRUPT_FLAG

def main():
    pid = os.getpid()
    print(f"Job started with PID {pid}")
    stop_event = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_spot_interruption,
        args=(FLAG_PATH,),
        kwargs={"stop_event": stop_event},
        daemon=True,
    )
    monitor_thread.start()
    run(stop_event=stop_event, interrupt_flag_path=FLAG_PATH)

if __name__ == "__main__":
    main()
