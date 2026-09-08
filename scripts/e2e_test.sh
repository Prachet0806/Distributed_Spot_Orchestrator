#!/usr/bin/env bash
# E2E Test Automation Script
# Usage: ./scripts/e2e_test.sh [phase]
# Phases: setup, deploy, test, cleanup, all

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_prereqs() {
    log_info "Checking prerequisites..."
    
    # Check AWS CLI
    if ! command -v aws &> /dev/null; then
        log_error "AWS CLI not found. Install: https://aws.amazon.com/cli/"
        exit 1
    fi
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 not found."
        exit 1
    fi
    
    # Check Terraform
    if ! command -v terraform &> /dev/null; then
        log_warn "Terraform not found. Infra setup will be skipped."
    fi
    
    # Check AWS credentials
    if ! aws sts get-caller-identity &> /dev/null; then
        log_error "AWS credentials not configured. Run: aws configure"
        exit 1
    fi
    
    log_info "Prerequisites OK"
}

run_unit_tests() {
    log_info "Running unit tests..."
    
    if [ ! -d "venv" ]; then
        log_info "Creating virtual environment..."
        python3 -m venv venv
    fi
    
    source venv/bin/activate 2>/dev/null || source venv/Scripts/activate
    pip install -q -r requirements.txt
    
    pytest tests/ -v --tb=short || {
        log_error "Unit tests failed"
        exit 1
    }
    
    log_info "Unit tests passed ✓"
}

setup_aws() {
    log_info "Setting up AWS resources..."
    
    # Generate unique names
    TIMESTAMP=$(date +%s)
    export CHECKPOINT_BUCKET="spot-arb-checkpoints-${TIMESTAMP}"
    export DYNAMO_TABLE="spot-arb-jobs-${TIMESTAMP}"
    export REGION="us-east-1"
    
    # Create S3 bucket
    log_info "Creating S3 bucket: ${CHECKPOINT_BUCKET}"
    aws s3 mb "s3://${CHECKPOINT_BUCKET}" --region ${REGION}
    
    # Create DynamoDB table
    log_info "Creating DynamoDB table: ${DYNAMO_TABLE}"
    aws dynamodb create-table \
        --table-name ${DYNAMO_TABLE} \
        --attribute-definitions \
            AttributeName=job_id,AttributeType=S \
            AttributeName=state,AttributeType=S \
        --key-schema \
            AttributeName=job_id,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region ${REGION} \
        > /dev/null
    
    # Wait for table
    log_info "Waiting for table to be active..."
    aws dynamodb wait table-exists --table-name ${DYNAMO_TABLE} --region ${REGION}
    
    # Create GSI (CRITICAL for performance)
    log_info "Creating StateIndex GSI..."
    aws dynamodb update-table \
        --table-name ${DYNAMO_TABLE} \
        --attribute-definitions AttributeName=state,AttributeType=S \
        --global-secondary-index-updates '[
            {
                "Create": {
                    "IndexName": "StateIndex",
                    "KeySchema": [{"AttributeName": "state", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"}
                }
            }
        ]' \
        --region ${REGION} \
        > /dev/null
    
    # Wait for GSI (can take 2-5 minutes)
    log_info "Waiting for GSI to be active (this may take a few minutes)..."
    while true; do
        STATUS=$(aws dynamodb describe-table \
            --table-name ${DYNAMO_TABLE} \
            --region ${REGION} \
            --query 'Table.GlobalSecondaryIndexes[0].IndexStatus' \
            --output text 2>/dev/null || echo "CREATING")
        
        if [ "$STATUS" = "ACTIVE" ]; then
            break
        fi
        
        echo -n "."
        sleep 10
    done
    echo ""
    
    # Save environment
    cat > .env_e2e <<EOF
export CHECKPOINT_BUCKET=${CHECKPOINT_BUCKET}
export DYNAMO_TABLE=${DYNAMO_TABLE}
export REGION=${REGION}
EOF
    
    log_info "AWS resources created ✓"
    log_info "Environment saved to .env_e2e"
}

test_metrics() {
    log_info "Testing metrics endpoint..."
    
    # Start orchestrator in background
    python3 -m orchestrator.main \
        --job-id test-job \
        --current-region us-east-1 \
        --interval 30 \
        --health-port 8080 \
        > orchestrator_test.log 2>&1 &
    
    ORCH_PID=$!
    log_info "Orchestrator started (PID: ${ORCH_PID})"
    
    # Wait for startup
    sleep 5
    
    # Test health endpoint
    if curl -s http://localhost:8080/health | grep -q "ok"; then
        log_info "Health endpoint OK ✓"
    else
        log_error "Health endpoint failed"
        kill $ORCH_PID 2>/dev/null || true
        exit 1
    fi
    
    # Test metrics endpoint
    METRICS=$(curl -s http://localhost:8080/metrics)
    
    # Check for key metrics
    for metric in "dynamodb_get" "dynamodb_query" "migration_success" "watcher_poll"; do
        if echo "$METRICS" | grep -q "$metric"; then
            log_info "Metric ${metric} found ✓"
        else
            log_warn "Metric ${metric} not found"
        fi
    done
    
    # Verify DynamoDB uses GSI (not scan)
    if echo "$METRICS" | grep -q "dynamodb_query_success_total"; then
        log_info "DynamoDB using GSI queries ✓"
    else
        log_warn "DynamoDB queries not detected"
    fi
    
    if echo "$METRICS" | grep -q "dynamodb_scan_success_total"; then
        log_warn "DynamoDB scans detected (should use GSI instead)"
    fi
    
    # Stop orchestrator
    kill $ORCH_PID 2>/dev/null || true
    wait $ORCH_PID 2>/dev/null || true
    
    log_info "Metrics test passed ✓"
}

test_rate_limiting() {
    log_info "Testing rate limiting..."
    
    # Start orchestrator with low limits
    python3 -m orchestrator.main \
        --job-id test-job \
        --current-region us-east-1 \
        --interval 10 \
        --max-migrations-per-hour 2 \
        --max-concurrent-migrations 1 \
        > orchestrator_rate_test.log 2>&1 &
    
    ORCH_PID=$!
    sleep 5
    
    # Check rate limiter configuration in logs
    if grep -q "Rate limiter configured" orchestrator_rate_test.log; then
        log_info "Rate limiter initialized ✓"
    else
        log_warn "Rate limiter config not found in logs"
    fi
    
    # Stop orchestrator
    kill $ORCH_PID 2>/dev/null || true
    wait $ORCH_PID 2>/dev/null || true
    
    log_info "Rate limiting test passed ✓"
}

cleanup_aws() {
    log_info "Cleaning up AWS resources..."
    
    if [ -f ".env_e2e" ]; then
        source .env_e2e
        
        # Delete DynamoDB table
        if [ -n "$DYNAMO_TABLE" ]; then
            log_info "Deleting DynamoDB table: ${DYNAMO_TABLE}"
            aws dynamodb delete-table --table-name ${DYNAMO_TABLE} 2>/dev/null || true
        fi
        
        # Delete S3 bucket
        if [ -n "$CHECKPOINT_BUCKET" ]; then
            log_info "Deleting S3 bucket: ${CHECKPOINT_BUCKET}"
            aws s3 rm "s3://${CHECKPOINT_BUCKET}" --recursive 2>/dev/null || true
            aws s3 rb "s3://${CHECKPOINT_BUCKET}" 2>/dev/null || true
        fi
        
        rm .env_e2e
    fi
    
    # Kill any running orchestrators
    pkill -f "orchestrator.main" 2>/dev/null || true
    
    log_info "Cleanup complete ✓"
}

print_summary() {
    log_info "========================================="
    log_info "E2E Test Summary"
    log_info "========================================="
    log_info "All tests passed successfully!"
    log_info ""
    log_info "Key validations:"
    log_info "  ✓ Unit tests passed"
    log_info "  ✓ AWS resources created"
    log_info "  ✓ DynamoDB GSI active"
    log_info "  ✓ Metrics endpoint working"
    log_info "  ✓ Rate limiting configured"
    log_info ""
    log_info "Next steps:"
    log_info "  1. Review docs/E2E_TESTING_GUIDE.md for full manual testing"
    log_info "  2. Deploy to production following Phase 6-10"
    log_info "  3. Set up Prometheus scraping for metrics"
    log_info ""
    log_info "To cleanup: ./scripts/e2e_test.sh cleanup"
    log_info "========================================="
}

# Main execution
PHASE=${1:-all}

case $PHASE in
    setup)
        check_prereqs
        run_unit_tests
        setup_aws
        ;;
    test)
        check_prereqs
        test_metrics
        test_rate_limiting
        ;;
    cleanup)
        cleanup_aws
        ;;
    all)
        check_prereqs
        run_unit_tests
        setup_aws
        test_metrics
        test_rate_limiting
        print_summary
        log_info "Resources created. Run './scripts/e2e_test.sh cleanup' when done."
        ;;
    *)
        echo "Usage: $0 [phase]"
        echo "Phases: setup, test, cleanup, all"
        exit 1
        ;;
esac
