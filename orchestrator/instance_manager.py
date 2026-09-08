# orchestrator/instance_manager.py
import boto3
import uuid
import logging

logger = logging.getLogger(__name__)


def provision_instance(
    region: str,
    ami_id: str,
    security_group_id: str,
    key_name: str,
    instance_type: str,
    max_spot_price: str | None = None,
    profile: str | None = None,
    timeout: int = 300,
    idempotency_token: str | None = None,
):
    """
    Provision a spot instance and return (instance_id, public_ip, public_dns).
    
    Args:
        region: AWS region
        ami_id: AMI ID for the instance
        security_group_id: Security group ID
        key_name: SSH key pair name
        instance_type: EC2 instance type
        max_spot_price: Maximum spot price (optional)
        profile: AWS profile name (optional)
        timeout: Timeout for instance to become running (seconds)
        idempotency_token: Token for idempotent provisioning (optional, auto-generated if None)
        
    Returns:
        Tuple of (instance_id, public_ip, public_dns)
        
    Raises:
        ValueError: If inputs are invalid
        RuntimeError: If provisioning fails
    """
    # Input validation
    if not region or not isinstance(region, str):
        raise ValueError(f"Invalid region: {region}")
    if not ami_id or not ami_id.startswith("ami-"):
        raise ValueError(f"Invalid AMI ID: {ami_id}")
    if not security_group_id or not security_group_id.startswith("sg-"):
        raise ValueError(f"Invalid security group ID: {security_group_id}")
    if not key_name or not isinstance(key_name, str):
        raise ValueError(f"Invalid key name: {key_name}")
    if not instance_type or not isinstance(instance_type, str):
        raise ValueError(f"Invalid instance type: {instance_type}")
    if timeout < 60 or timeout > 600:
        raise ValueError(f"Invalid timeout: {timeout} (must be 60-600)")
    
    # Generate idempotency token if not provided
    if not idempotency_token:
        idempotency_token = str(uuid.uuid4())
    
    logger.info(f"Provisioning spot instance in {region} (token: {idempotency_token[:8]})")
    
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    ec2 = session.client("ec2", region_name=region)

    launch_spec = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "KeyName": key_name,
        "SecurityGroupIds": [security_group_id],
        "InstanceMarketOptions": {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
            },
        },
        "ClientToken": idempotency_token,  # Idempotent provisioning
    }
    if max_spot_price:
        launch_spec["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] = max_spot_price

    try:
        resp = ec2.run_instances(MinCount=1, MaxCount=1, **launch_spec)
        instance = resp["Instances"][0]
        instance_id = instance["InstanceId"]
        
        logger.info(f"Instance requested: {instance_id}. Waiting for running state...")

        # Wait with timeout
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={'Delay': 15, 'MaxAttempts': timeout // 15}
        )

        desc = ec2.describe_instances(InstanceIds=[instance_id])
        inst = desc["Reservations"][0]["Instances"][0]
        public_ip = inst.get("PublicIpAddress")
        public_dns = inst.get("PublicDnsName")
        
        if not public_ip:
            raise RuntimeError(f"Instance {instance_id} has no public IP")
        
        logger.info(f"Instance {instance_id} ready at {public_ip}")
        return instance_id, public_ip, public_dns
        
    except Exception as e:
        logger.error(f"Failed to provision instance: {e}")
        raise RuntimeError(f"Instance provisioning failed: {e}")

