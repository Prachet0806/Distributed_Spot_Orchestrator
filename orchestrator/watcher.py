# orchestrator/watcher.py
import boto3
import time
import statistics

class SpotPriceWatcher:
    def __init__(self, regions, instance_type):
        self.regions = regions
        self.instance_type = instance_type
        self.history = {r: [] for r in regions}

    def poll(self):
        results = {}
        for region in self.regions:
            ec2 = boto3.client("ec2", region_name=region)
            prices = ec2.describe_spot_price_history(
                InstanceTypes=[self.instance_type],
                ProductDescriptions=["Linux/UNIX"],
                MaxResults=5
            )["SpotPriceHistory"]

            latest = float(prices[0]["SpotPrice"])
            self.history[region].append(latest)

            if len(self.history[region]) > 20:
                self.history[region].pop(0)

            volatility = (
                statistics.stdev(self.history[region])
                if len(self.history[region]) > 1 else 0.0
            )

            results[region] = {
                "price": latest,
                "volatility": volatility,
                "timestamp": time.time()
            }

        return results
