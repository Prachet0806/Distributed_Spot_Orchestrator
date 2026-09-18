"""Phase 6b: Terraform static guards (no terraform binary in CI).

Every `var.X` referenced must be declared; known-bad references
(`aws_iam_role.worker_role`) must stay fixed; V2 state resources,
KMS, and S3 hardening must be present.
"""
import re
from pathlib import Path

TF = Path("infra/aws")


def _read(name):
    return (TF / name).read_text()


def _vars_declared():
    return set(re.findall(r'variable\s+"([^"]+)"', _read("variables.tf")))


def _vars_referenced():
    refs = set()
    for path in TF.glob("*.tf"):
        refs |= set(re.findall(r'var\.([a-z_]+)', path.read_text()))
    return refs


def test_all_referenced_vars_declared():
    assert _vars_referenced() <= _vars_declared(), (
        _vars_referenced() - _vars_declared())


def test_role_reference_fixed():
    assert "aws_iam_role.worker_role" not in _read("outputs.tf")
    assert "spot_worker_role" in _read("outputs.tf")


def test_required_vars_present():
    for var in ("target_region", "ami_id", "instance_type", "ssh_key_name",
                "ssh_user", "max_spot_price", "my_ip", "dynamodb_table_name",
                "kms_key_arn", "environment"):
        assert var in _vars_declared(), var


def test_state_tables_present():
    dynamo = _read("dynamodb.tf")
    for table in ("job_registry", "candidate_pools", "plans", "event_ledger",
                  "checkpoints", "audit", "migration_history"):
        assert f'resource "aws_dynamodb_table" "{table}"' in dynamo, table
    assert '"StateIndex"' in dynamo
    assert '"event_id"' in dynamo and '"consumer_id"' in dynamo  # composite ledger key
    assert "LineageIndex" in dynamo


def test_kms_and_s3_hardening_present():
    assert 'resource "aws_kms_key" "checkpoint_key"' in _read("kms.tf")
    main = _read("main.tf")
    assert "aws_s3_bucket_versioning" in main
    assert "aws_s3_bucket_server_side_encryption_configuration" in main
    assert "aws_kms_key.checkpoint_key.arn" in main
    assert "abort_incomplete_multipart_upload" in main
    assert "aws_s3_bucket_public_access_block" in main
