# orchestrator/watcher.py
import boto3
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from orchestrator.constants import MAX_PRICE_HISTORY, DEFAULT_SPOT_PRICE_SAMPLES

logger = logging.getLogger(__name__)


class SpotPriceWatcher:
    """
    Spot price watcher with optimizations:
    - Cached boto3 clients (no recreation overhead)
    - Parallel region polling (3x faster for 3 regions)
    """
    
    def __init__(self, regions, instance_type, max_workers=None):
        """
        Initialize spot price watcher.
        
        Args:
            regions: List of AWS regions to monitor
            instance_type: EC2 instance type to query prices for
            max_workers: Max parallel workers (default: number of regions)
        """
        self.regions = regions
        self.instance_type = instance_type
        self.history = {r: [] for r in regions}
        
        # Cache EC2 clients per region - avoids recreation overhead
        logger.info(f"Initializing EC2 clients for {len(regions)} regions")
        self.clients = {r: boto3.client("ec2", region_name=r) for r in regions}
        
        # Thread pool for parallel polling
        self.max_workers = max_workers or min(len(regions), 10)

    def poll(self):
        """
        Poll spot prices for all regions in parallel.
        
        Returns:
            Dict mapping region to price data
        """
        results = {}
        
        # Use ThreadPoolExecutor for parallel API calls
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all region polls
            future_to_region = {
                executor.submit(self._poll_region, region): region
                for region in self.regions
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    results[region] = future.result()
                except Exception as e:
                    logger.error(f"Failed to poll {region}: {e}")
                    # Return stale data or default on error
                    results[region] = {
                        "price": self.history[region][-1] if self.history[region] else 999.0,
                        "volatility": 0.0,
                        "timestamp": time.time(),
                        "error": str(e)
                    }
        
        return results
    
    def _poll_region(self, region):
        """
        Poll spot price for a single region.
        
        Args:
            region: AWS region name
            
        Returns:
            Dict with price, volatility, and timestamp
        """
        ec2 = self.clients[region]  # Use cached client
        
        prices = ec2.describe_spot_price_history(
            InstanceTypes=[self.instance_type],
            ProductDescriptions=["Linux/UNIX"],
            MaxResults=DEFAULT_SPOT_PRICE_SAMPLES
        )["SpotPriceHistory"]
        
        if not prices:
            raise RuntimeError(f"No price data available for {region}")
        
        latest = float(prices[0]["SpotPrice"])
        
        # Update history
        self.history[region].append(latest)
        if len(self.history[region]) > MAX_PRICE_HISTORY:
            self.history[region].pop(0)
        
        # Calculate volatility
        volatility = (
            statistics.stdev(self.history[region])
            if len(self.history[region]) > 1 else 0.0
        )
        
        return {
            "price": latest,
            "volatility": volatility,
            "timestamp": time.time()
        }
