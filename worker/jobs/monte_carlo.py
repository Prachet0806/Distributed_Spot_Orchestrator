# worker/jobs/monte_carlo.py
import os
import random
import time

def run(iterations=10_000_000, stop_event=None, interrupt_flag_path=None):
    inside = 0
    for i in range(iterations):
        if stop_event is not None and stop_event.is_set():
            print("Spot interruption detected; exiting job loop.")
            return
        if interrupt_flag_path and os.path.exists(interrupt_flag_path):
            print("Spot interruption flag found; exiting job loop.")
            return
        x, y = random.random(), random.random()
        if x*x + y*y <= 1:
            inside += 1
        if i % 1_000_000 == 0:
            time.sleep(0.01)
    pi = (inside / iterations) * 4
    print(f"Estimated Pi = {pi}")
