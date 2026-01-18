# worker/jobs/monte_carlo.py
import random
import time

def run(iterations=10_000_000):
    inside = 0
    for i in range(iterations):
        x, y = random.random(), random.random()
        if x*x + y*y <= 1:
            inside += 1
        if i % 1_000_000 == 0:
            time.sleep(0.01)
    pi = (inside / iterations) * 4
    print(f"Estimated Pi = {pi}")
