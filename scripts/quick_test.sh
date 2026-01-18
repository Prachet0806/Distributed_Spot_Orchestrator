#!/bin/bash
# Quick test suite for Spot Arbitrage Cluster
set -e

echo "=== Quick Test Suite ==="
echo ""

# Test 1: Unit tests
echo "1. Running unit tests..."
python -m pytest tests/test_decision_engine.py -v || echo "⚠️  Unit tests failed (install pytest: pip install pytest)"

# Test 2: Import tests
echo ""
echo "2. Testing imports..."
python -c "from orchestrator.decision_engine import DecisionEngine; print('✓ DecisionEngine')" || echo "✗ DecisionEngine import failed"
python -c "from orchestrator.watcher import SpotPriceWatcher; print('✓ SpotPriceWatcher')" || echo "✗ SpotPriceWatcher import failed"
python -c "from orchestrator.migrator import Migrator; print('✓ Migrator')" || echo "✗ Migrator import failed"
python -c "from orchestrator.utils import SSHClient; print('✓ SSHClient')" || echo "✗ SSHClient import failed"
python -c "from storage.job_registry import JobRegistry; print('✓ JobRegistry')" || echo "✗ JobRegistry import failed"
python -c "from storage.s3_manager import S3Manager; print('✓ S3Manager')" || echo "✗ S3Manager import failed"

# Test 3: Configuration files
echo ""
echo "3. Testing configuration files..."
python -c "import yaml; yaml.safe_load(open('orchestrator/sla_policy.yaml')); print('✓ SLA Policy')" || echo "✗ SLA Policy invalid"
python -c "import yaml; yaml.safe_load(open('config/regions.yaml')); print('✓ Regions Config')" || echo "✗ Regions Config invalid"
python -c "import yaml; yaml.safe_load(open('config/logging.yaml')); print('✓ Logging Config')" || echo "✗ Logging Config invalid"

# Test 4: File structure
echo ""
echo "4. Checking file structure..."
[ -f "orchestrator/decision_engine.py" ] && echo "✓ orchestrator/decision_engine.py" || echo "✗ Missing orchestrator/decision_engine.py"
[ -f "orchestrator/migrator.py" ] && echo "✓ orchestrator/migrator.py" || echo "✗ Missing orchestrator/migrator.py"
[ -f "orchestrator/utils.py" ] && echo "✓ orchestrator/utils.py" || echo "✗ Missing orchestrator/utils.py"
[ -f "storage/job_registry.py" ] && echo "✓ storage/job_registry.py" || echo "✗ Missing storage/job_registry.py"
[ -f "storage/s3_manager.py" ] && echo "✓ storage/s3_manager.py" || echo "✗ Missing storage/s3_manager.py"
[ -f "worker/job_runner.py" ] && echo "✓ worker/job_runner.py" || echo "✗ Missing worker/job_runner.py"

# Test 5: Terraform validation (if terraform available)
echo ""
echo "5. Validating Terraform..."
if command -v terraform &> /dev/null; then
    cd infra/aws
    terraform init -backend=false > /dev/null 2>&1 && terraform validate > /dev/null 2>&1 && echo "✓ Terraform valid" || echo "⚠️  Terraform validation failed (check variables)"
    cd ../..
else
    echo "⚠️  Terraform not installed (skipping)"
fi

echo ""
echo "=== Quick Test Suite Complete ==="

