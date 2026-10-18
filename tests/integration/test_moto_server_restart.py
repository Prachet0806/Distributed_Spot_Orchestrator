"""Independent clients share durable state through a real HTTP service."""
import os
import socket
import subprocess
import sys

import boto3
from moto.server import ThreadedMotoServer


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_separate_processes_recover_operation_plan_and_artifact(monkeypatch, tmp_path):
    port = _port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    endpoint = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    env = {**os.environ, "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
           "SPOT_TEST_ENDPOINT": endpoint, "SPOT_TEST_ROOT": str(tmp_path)}
    try:
        ddb = boto3.resource("dynamodb", region_name="us-east-1", endpoint_url=endpoint)
        for name, key in (("spot_arbitrage_operations", "operation_key"),
                          ("spot_arbitrage_plans", "plan_id")):
            ddb.create_table(TableName=name, KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST")
        boto3.client("s3", region_name="us-east-1", endpoint_url=endpoint).create_bucket(
            Bucket="spot-restart-fixture")
        writer = """import os,boto3
from pathlib import Path
from storage.operation_store import OperationStore
from storage.plan_store import PlanStore
from storage.s3_manager import S3Manager
r=boto3.resource('dynamodb',region_name='us-east-1',endpoint_url=os.environ['SPOT_TEST_ENDPOINT'])
PlanStore(dynamodb_resource=r).put_plan_doc({'plan_id':'p','job_id':'j','migration_id':'m','plan_hash':'h','steps':[]})
OperationStore(dynamodb_resource=r).begin('p:provision',{'pool':'a'})
images=Path(os.environ['SPOT_TEST_ROOT'])/'images'; images.mkdir()
(images/'core-1.img').write_bytes(b'core')
(images/'inventory.img').write_bytes(b'inventory')
s3=boto3.client('s3',region_name='us-east-1',endpoint_url=os.environ['SPOT_TEST_ENDPOINT'])
S3Manager('spot-restart-fixture',s3_client=s3).upload('c',src=str(images))
"""
        reader = """import os,boto3
from pathlib import Path
from storage.operation_store import OperationStore
from storage.plan_store import PlanStore
from storage.s3_manager import S3Manager
r=boto3.resource('dynamodb',region_name='us-east-1',endpoint_url=os.environ['SPOT_TEST_ENDPOINT'])
assert PlanStore(dynamodb_resource=r).get_plan('p')['job_id']=='j'
op=OperationStore(dynamodb_resource=r).begin('p:provision',{'pool':'a'})
assert op['state']=='ISSUED' and op['newly_issued'] is False
s3=boto3.client('s3',region_name='us-east-1',endpoint_url=os.environ['SPOT_TEST_ENDPOINT'])
manager=S3Manager('spot-restart-fixture',s3_client=s3)
manifest=manager.manifest('c')
destination=Path(os.environ['SPOT_TEST_ROOT'])/'restored'
manager.download('c',dst=str(destination),expected_checksum=manifest['artifact']['sha256'])
assert (destination/'core-1.img').read_bytes()==b'core'
"""
        for program in (writer, reader):
            result = subprocess.run([sys.executable, "-c", program], env=env,
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.stop()
