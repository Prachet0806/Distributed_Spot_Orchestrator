# orchestrator/decision_engine.py
import yaml
from dataclasses import dataclass

@dataclass
class Decision:
    action: str
    target_region: str | None
    reason: str

class DecisionEngine:
    def __init__(self, sla_policy_path):
        with open(sla_policy_path) as f:
            self.policy = yaml.safe_load(f)

        # Workload thresholds: fallback if not in policy
        self.workload_thresholds = self.policy.get("workload_thresholds", {
            "short": None,           # never migrate
            "medium": 0.25,          # 25%
            "long": 0.12,            # 12%
            "stateful": 0.40,        # 40%
        })
        self.default_threshold = self.policy.get("price_spike_threshold", 0.01)

    def _threshold_for_job(self, job):
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

    def evaluate(self, prices, current_region, job=None):
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

        # If workload dictates "never migrate" (short) and no threshold, stay.
        if threshold is None:
            return Decision("STAY", None, "workload_short_no_migrate")

        if delta > threshold:
            return Decision("MIGRATE", target_region, "price_spike")

        return Decision("STAY", None, "within_threshold")
