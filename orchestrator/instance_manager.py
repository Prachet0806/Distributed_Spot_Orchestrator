# orchestrator/instance_manager.py
import boto3


def provision_instance(
    region: str,
    ami_id: str,
    security_group_id: str,
    key_name: str,
    instance_type: str,
    max_spot_price: str | None = None,
    profile: str | None = None,
):
    """
    Provision a spot instance and return (instance_id, public_ip, public_dns).
    """
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

    resp = ec2.run_instances(MinCount=1, MaxCount=1, **launch_spec)
    instance = resp["Instances"][0]
    instance_id = instance["InstanceId"]

    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    desc = ec2.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]
    public_ip = inst.get("PublicIpAddress")
    public_dns = inst.get("PublicDnsName")

    return instance_id, public_ip, public_dns

