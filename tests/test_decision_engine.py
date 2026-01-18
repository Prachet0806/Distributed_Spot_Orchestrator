import unittest
from orchestrator.decision_engine import DecisionEngine, Decision

class TestDecisionEngine(unittest.TestCase):
    def setUp(self):
        # Use existing policy file (relative to project root)
        self.engine = DecisionEngine("orchestrator/sla_policy.yaml")
        # Inject mock policy data to avoid relying on file contents
        self.engine.policy = {"price_spike_threshold": 0.05}

    def test_stay_if_cheapest(self):
        prices = {
            "us-east-1": {"price": 0.10, "volatility": 0.0},
            "us-west-2": {"price": 0.20, "volatility": 0.0}
        }
        # We are in us-east-1 (cheapest)
        decision = self.engine.evaluate(prices, "us-east-1")
        self.assertEqual(decision.action, "STAY")

    def test_migrate_if_spike(self):
        prices = {
            "us-east-1": {"price": 0.50, "volatility": 0.0}, # Spike!
            "us-west-2": {"price": 0.10, "volatility": 0.0}
        }
        # We are in us-east-1 (expensive)
        decision = self.engine.evaluate(prices, "us-east-1")
        self.assertEqual(decision.action, "MIGRATE")
        self.assertEqual(decision.target_region, "us-west-2")

    def test_stay_if_saving_small(self):
        prices = {
            "us-east-1": {"price": 0.12, "volatility": 0.0}, 
            "us-west-2": {"price": 0.10, "volatility": 0.0}
        }
        # Difference is 0.02, Threshold is 0.05 -> STAY
        decision = self.engine.evaluate(prices, "us-east-1")
        self.assertEqual(decision.action, "STAY")

if __name__ == '__main__':
    unittest.main()