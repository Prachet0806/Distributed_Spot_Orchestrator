# orchestrator/decision_engine.py
"""V1 LEGACY FROZEN — do not extend. Use V2 PolicyEngine
(`orchestrator/policy_engine.py`). Kept for `--engine v1` compat (ADR-024)."""
import warnings

warnings.warn(
    "orchestrator.decision_engine.DecisionEngine is V1 legacy (frozen). "
    "Use V2 PolicyEngine.",
    DeprecationWarning,
    stacklevel=2,
)
import yaml
from dataclasses import dataclass
from typing import Optional, Dict, Any

@dataclass
class Decision:
    """Migration decision with action, target, and reasoning."""
    action: str  # "MIGRATE" or "STAY"
    target_region: Optional[str]
    reason: str

class DecisionEngine:
    """
    SLA policy-driven decision engine for spot instance migrations.
    
    Evaluates spot prices against configured thresholds and workload types
    to determine if migration is warranted.
    """
    
    def __init__(self, sla_policy_path: str, optimization_mode: Optional[str] = None):
        with open(sla_policy_path) as f:
            self.policy = yaml.safe_load(f)
        self.optimization_mode = optimization_mode or "cost_first"

        # Workload thresholds: fallback if not in policy
        self.workload_thresholds = self.policy.get("workload_thresholds", {
            "short": None,           # never migrate
            "medium": 0.25,          # 25%
            "long": 0.12,            # 12%
            "stateful": 0.40,        # 40%
        })
        self.default_threshold = self.policy.get("price_spike_threshold", 0.01)

    def _threshold_for_job(self, job: Optional[Dict[str, Any]]) -> Optional[float]:
        """
        Get migration threshold for a job based on its workload type.
        
        Args:
            job: Job dictionary with optional workload_type
            
        Returns:
            Threshold value or None (never migrate)
        """
        if not job:
            return self.default_threshold
        workload_type = job.get("workload_type")
        if not workload_type:
            return self.default_threshold
        wt = str(workload_type).lower()
        wt_threshold = self.workload_thresholds.get(wt)

        # If workload is "short", treat as do-not-migrate unless price spike exceeds default *and* workload threshold is None
        if wt == "short":
            return None  # never migrate unless caller overrides
        if wt_threshold is None:
            return self.default_threshold
        # Use the max of workload-specific threshold and default spike threshold
        return max(wt_threshold, self.default_threshold)

    def _apply_optimization_mode(self, threshold: Optional[float]) -> Optional[float]:
        """Apply optimization mode modifier to threshold."""
        if threshold is None:
            return None
        mode = str(self.optimization_mode or "cost_first").lower()
        if mode == "availability_first":
            return threshold * 1.5
        return threshold

    def evaluate(
        self, 
        prices: Dict[str, Dict[str, Any]], 
        current_region: str, 
        job: Optional[Dict[str, Any]] = None
    ) -> Decision:
        current_price = prices[current_region]["price"]

        cheapest = min(
            prices.items(),
            key=lambda x: x[1]["price"]
        )

        target_region, data = cheapest

        if target_region == current_region:
            return Decision("STAY", None, "already_cheapest")

        delta = current_price - data["price"]

        threshold = self._threshold_for_job(job)
        threshold = self._apply_optimization_mode(threshold)

        # If workload dictates "never migrate" (short) and no threshold, stay.
        if threshold is None:
            return Decision("STAY", None, "workload_short_no_migrate")

        if delta > threshold:
            return Decision("MIGRATE", target_region, "price_spike")

        return Decision("STAY", None, "within_threshold")
