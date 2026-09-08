# orchestrator/rate_limiter.py
"""
Rate limiter for multi-job orchestration to prevent API throttling and resource exhaustion.
"""
import time
import threading
from collections import deque
from typing import Optional


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter with concurrent operation tracking.
    
    Prevents both:
    1. Too many operations per time window (API throttling)
    2. Too many concurrent operations (resource exhaustion)
    """
    
    def __init__(
        self,
        max_per_hour: int,
        max_concurrent: int,
        min_interval_seconds: float = 0
    ):
        """
        Initialize rate limiter.
        
        Args:
            max_per_hour: Maximum operations per hour
            max_concurrent: Maximum concurrent operations
            min_interval_seconds: Minimum seconds between operations
        """
        self.max_per_hour = max_per_hour
        self.max_concurrent = max_concurrent
        self.min_interval_seconds = min_interval_seconds
        
        self.lock = threading.Lock()
        self.timestamps = deque()  # Recent operation timestamps
        self.active_count = 0       # Currently active operations
        self.last_op_time = 0       # Last operation timestamp
    
    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire permission to perform an operation.
        
        Args:
            timeout: Max seconds to wait (None = wait forever, 0 = no wait)
            
        Returns:
            True if acquired, False if timeout/would block
        """
        start = time.time()
        
        while True:
            with self.lock:
                now = time.time()
                
                # Remove timestamps older than 1 hour
                hour_ago = now - 3600
                while self.timestamps and self.timestamps[0] < hour_ago:
                    self.timestamps.popleft()
                
                # Check all constraints
                can_proceed = (
                    len(self.timestamps) < self.max_per_hour and
                    self.active_count < self.max_concurrent and
                    (now - self.last_op_time) >= self.min_interval_seconds
                )
                
                if can_proceed:
                    self.timestamps.append(now)
                    self.active_count += 1
                    self.last_op_time = now
                    return True
            
            # Check timeout
            if timeout is not None:
                elapsed = time.time() - start
                if elapsed >= timeout:
                    return False
                if timeout == 0:
                    return False
            
            # Brief sleep before retry
            time.sleep(0.1)
    
    def release(self):
        """Release an active operation slot."""
        with self.lock:
            if self.active_count > 0:
                self.active_count -= 1
    
    def get_stats(self) -> dict:
        """Get current rate limiter statistics."""
        with self.lock:
            now = time.time()
            hour_ago = now - 3600
            recent_count = sum(1 for ts in self.timestamps if ts > hour_ago)
            
            return {
                "active_count": self.active_count,
                "max_concurrent": self.max_concurrent,
                "operations_last_hour": recent_count,
                "max_per_hour": self.max_per_hour,
                "utilization_pct": (recent_count / self.max_per_hour) * 100,
            }


class RateLimitContext:
    """Context manager for rate-limited operations."""
    
    def __init__(self, limiter: TokenBucketRateLimiter, timeout: Optional[float] = None):
        self.limiter = limiter
        self.timeout = timeout
        self.acquired = False
    
    def __enter__(self):
        self.acquired = self.limiter.acquire(timeout=self.timeout)
        if not self.acquired:
            raise RuntimeError("Rate limit: Could not acquire within timeout")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            self.limiter.release()
        return False
