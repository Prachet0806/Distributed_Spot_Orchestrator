import argparse
import time
import boto3


def provision_instance(region, ami_id, key_name, security_group_id, instance_type, max_spot_price=None, profile=None):
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
    }
    if max_spot_price:
        launch_spec["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] = max_spot_price

    resp = ec2.run_instances(
        MinCount=1,
        MaxCount=1,
        **launch_spec,
    )
    instance = resp["Instances"][0]
    instance_id = instance["InstanceId"]

    print(f"Instance requested: {instance_id}. Waiting for running state...")

    # Wait until running
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    # Fetch details
    desc = ec2.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]
    public_ip = inst.get("PublicIpAddress")
    public_dns = inst.get("PublicDnsName")

    print("✅ Instance ready")
    print(f"ID: {instance_id}")
    print(f"Public IP: {public_ip}")
    print(f"Public DNS: {public_dns}")
    return instance_id, public_ip, public_dns


def main():
    parser = argparse.ArgumentParser(description="Provision a target spot instance in a region.")
    parser.add_argument("--region", required=True, help="AWS region for the target instance")
    parser.add_argument("--ami-id", required=True, help="AMI ID for the target instance")
    parser.add_argument("--key-name", required=True, help="EC2 key pair name (must exist in the region)")
    parser.add_argument("--security-group-id", required=True, help="Security group ID allowing SSH from orchestrator")
    parser.add_argument("--instance-type", default="t3.micro", help="Instance type (default: t3.micro)")
    parser.add_argument("--max-spot-price", default=None, help="Optional max spot price")
    parser.add_argument("--profile", default=None, help="Optional AWS CLI profile")
    args = parser.parse_args()

    provision_instance(
        region=args.region,
        ami_id=args.ami_id,
        key_name=args.key_name,
        security_group_id=args.security_group_id,
        instance_type=args.instance_type,
        max_spot_price=args.max_spot_price,
        profile=args.profile,
    )


if __name__ == "__main__":
    main()

