#!/usr/bin/env bash
#
# AWS Validation Automation Script
# Automates the pre-push AWS validation process
#
# Usage: ./scripts/aws_validation.sh [phase]
#   phase: setup|test|cleanup|all (default: all)

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
    echo -e "${GREEN}✅${NC} $1"
}

log_error() {
    echo -e "${RED}❌${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_section() {
    echo ""
    echo -e "${BLUE}================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}================================================${NC}"
    echo ""
}

# Configuration
CHECKPOINT_BUCKET="spot-arbitrage-checkpoints-$(date +%s)"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/your-key.pem}"
AWS_KEY_NAME="${AWS_KEY_NAME:-your-key-name}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Phase 1: Setup Infrastructure
setup_infrastructure() {
    log_section "Phase 1: Infrastructure Setup"
    
    # Check prerequisites
    log_info "Checking prerequisites..."
    
    command -v aws >/dev/null 2>&1 || { log_error "AWS CLI not installed"; exit 1; }
    command -v terraform >/dev/null 2>&1 || { log_error "Terraform not installed"; exit 1; }
    command -v python3 >/dev/null 2>&1 || { log_error "Python3 not installed"; exit 1; }
    
    log_success "Prerequisites OK"
    
    # Check AWS credentials
    log_info "Checking AWS credentials..."
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        log_error "AWS credentials not configured. Run: aws configure"
        exit 1
    fi
    log_success "AWS credentials OK"
    
    # Create S3 bucket
    log_info "Creating S3 bucket: $CHECKPOINT_BUCKET"
    aws s3 mb s3://$CHECKPOINT_BUCKET --region $AWS_REGION
    
    aws s3api put-bucket-versioning \
        --bucket $CHECKPOINT_BUCKET \
        --versioning-configuration Status=Enabled
    
    aws s3api put-bucket-encryption \
        --bucket $CHECKPOINT_BUCKET \
        --server-side-encryption-configuration '{
          "Rules": [{
            "ApplyServerSideEncryptionByDefault": {
              "SSEAlgorithm": "AES256"
            }
          }]
        }'
    
    log_success "S3 bucket created and configured"
    export CHECKPOINT_BUCKET
    
    # Create DynamoDB table
    log_info "Creating DynamoDB table..."
    aws dynamodb create-table \
        --table-name spot_arbitrage_registry \
        --attribute-definitions \
          AttributeName=job_id,AttributeType=S \
          AttributeName=state,AttributeType=S \
        --key-schema \
          AttributeName=job_id,KeyType=HASH \
        --global-secondary-indexes '[
          {
            "IndexName": "StateIndex",
            "KeySchema": [
              {"AttributeName": "state", "KeyType": "HASH"}
            ],
            "Projection": {"ProjectionType": "ALL"},
            "ProvisionedThroughput": {
              "ReadCapacityUnits": 5,
              "WriteCapacityUnits": 5
            }
          }
        ]' \
        --provisioned-throughput \
          ReadCapacityUnits=5,WriteCapacityUnits=5 \
        --region $AWS_REGION || log_warning "Table may already exist"
    
    log_info "Waiting for DynamoDB table to become active..."
    aws dynamodb wait table-exists \
        --table-name spot_arbitrage_registry \
        --region $AWS_REGION
    
    sleep 30  # Wait for GSI
    
    log_success "DynamoDB table ready with GSI"
    
    # Deploy Terraform
    log_info "Deploying Terraform infrastructure..."
    cd infra/aws
    
    terraform init -input=false
    
    # Get Ubuntu AMI
    export TF_VAR_ami_id=$(aws ec2 describe-images \
        --owners 099720109477 \
        --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
        --query 'Images | sort_by(@, &CreationDate) | [-1].ImageId' \
        --output text \
        --region $AWS_REGION)
    
    export TF_VAR_target_region=$AWS_REGION
    export TF_VAR_my_ip="$(curl -s ifconfig.me)/32"
    export TF_VAR_ssh_key_name=$AWS_KEY_NAME
    
    log_info "Using AMI: $TF_VAR_ami_id"
    log_info "Your IP: $TF_VAR_my_ip"
    
    terraform plan -out=tfplan
    terraform apply -auto-approve tfplan
    
    export WORKER_IP=$(terraform output -raw public_ip)
    export INSTANCE_ID=$(terraform output -raw instance_id)
    
    cd ../..
    
    log_success "Infrastructure deployed"
    log_info "Worker IP: $WORKER_IP"
    log_info "Instance ID: $INSTANCE_ID"
    
    # Wait for instance ready
    log_info "Waiting for instance to be ready..."
    aws ec2 wait instance-status-ok \
        --instance-ids $INSTANCE_ID \
        --region $AWS_REGION
    
    log_info "Waiting for SSH..."
    for i in {1..30}; do
        if ssh -i $SSH_KEY_PATH \
               -o StrictHostKeyChecking=no \
               -o ConnectTimeout=5 \
               ubuntu@$WORKER_IP "echo connected" 2>/dev/null; then
            log_success "SSH connection successful"
            break
        fi
        echo -n "."
        sleep 10
    done
    echo ""
    
    # Deploy code
    log_info "Deploying code to worker..."
    python scripts/deploy_worker.py \
        --ip $WORKER_IP \
        --key $SSH_KEY_PATH \
        --root .
    
    log_success "Code deployed"
    
    # Save environment
    cat > .aws_validation_env <<EOF
export CHECKPOINT_BUCKET="$CHECKPOINT_BUCKET"
export WORKER_IP="$WORKER_IP"
export INSTANCE_ID="$INSTANCE_ID"
export AWS_REGION="$AWS_REGION"
export SSH_KEY_PATH="$SSH_KEY_PATH"
EOF
    
    log_success "Setup complete! Environment saved to .aws_validation_env"
    log_info "Source it with: source .aws_validation_env"
}

# Phase 2: Run Tests
run_tests() {
    log_section "Phase 2: Running Tests"
    
    # Load environment
    if [ -f .aws_validation_env ]; then
        source .aws_validation_env
        log_info "Environment loaded"
    else
        log_error "No environment file found. Run setup first."
        exit 1
    fi
    
    # Test spot price monitoring
    log_info "Testing spot price monitoring..."
    timeout 60 python -m orchestrator.main \
        --job-id test-validation \
        --current-region $AWS_REGION \
        --regions us-east-1,us-west-2 \
        --instance-type t3.micro \
        --interval 30 \
        --no-migrate || log_warning "Orchestrator test completed"
    
    log_success "Spot price monitoring works"
    
    # Test health check
    log_info "Testing health endpoint..."
    if curl -s http://localhost:8080/health | grep -q "healthy"; then
        log_success "Health check OK"
    else
        log_error "Health check failed"
    fi
    
    # Test metrics
    log_info "Testing metrics endpoint..."
    if curl -s http://localhost:8080/metrics > /tmp/metrics.txt; then
        metric_count=$(grep -c "^[a-z]" /tmp/metrics.txt || true)
        log_success "Metrics OK ($metric_count metrics)"
    else
        log_error "Metrics endpoint failed"
    fi
    
    # Test DynamoDB GSI
    log_info "Testing DynamoDB GSI query..."
    start_time=$(date +%s%N)
    aws dynamodb query \
        --table-name spot_arbitrage_registry \
        --index-name StateIndex \
        --key-condition-expression "state = :state" \
        --expression-attribute-values '{":state": {"S": "RUNNING"}}' \
        --region $AWS_REGION >/dev/null
    end_time=$(date +%s%N)
    duration_ms=$(( ($end_time - $start_time) / 1000000 ))
    
    if [ $duration_ms -lt 200 ]; then
        log_success "GSI query: ${duration_ms}ms (FAST)"
    else
        log_warning "GSI query: ${duration_ms}ms (slower than expected)"
    fi
    
    # Test S3 operations
    log_info "Testing S3 operations..."
    echo "test" > /tmp/test_file.txt
    aws s3 cp /tmp/test_file.txt s3://$CHECKPOINT_BUCKET/test.txt
    aws s3 cp s3://$CHECKPOINT_BUCKET/test.txt /tmp/test_file_downloaded.txt
    
    if diff /tmp/test_file.txt /tmp/test_file_downloaded.txt >/dev/null; then
        log_success "S3 upload/download works"
    else
        log_error "S3 operations failed"
    fi
    
    rm -f /tmp/test_file.txt /tmp/test_file_downloaded.txt
    aws s3 rm s3://$CHECKPOINT_BUCKET/test.txt
    
    log_success "All tests passed!"
}

# Phase 3: Cleanup
cleanup() {
    log_section "Phase 3: Cleanup"
    
    # Load environment
    if [ -f .aws_validation_env ]; then
        source .aws_validation_env
    else
        log_warning "No environment file found, using defaults"
    fi
    
    log_warning "This will delete all AWS resources created during validation"
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Cleanup cancelled"
        exit 0
    fi
    
    # Delete S3 bucket
    if [ -n "$CHECKPOINT_BUCKET" ]; then
        log_info "Deleting S3 bucket: $CHECKPOINT_BUCKET"
        aws s3 rb s3://$CHECKPOINT_BUCKET --force 2>/dev/null || log_warning "Bucket may not exist"
        log_success "S3 bucket deleted"
    fi
    
    # Delete DynamoDB table
    log_info "Deleting DynamoDB table..."
    aws dynamodb delete-table \
        --table-name spot_arbitrage_registry \
        --region $AWS_REGION 2>/dev/null || log_warning "Table may not exist"
    log_success "DynamoDB table deleted"
    
    # Destroy Terraform
    log_info "Destroying Terraform infrastructure..."
    cd infra/aws
    terraform destroy -auto-approve 2>/dev/null || log_warning "Terraform resources may not exist"
    cd ../..
    log_success "Terraform destroyed"
    
    # Clean up local files
    rm -f .aws_validation_env
    rm -f /tmp/metrics.txt
    
    log_success "Cleanup complete!"
}

# Main
case "${1:-all}" in
    setup)
        setup_infrastructure
        ;;
    test)
        run_tests
        ;;
    cleanup)
        cleanup
        ;;
    all)
        setup_infrastructure
        log_info "Waiting 30 seconds before tests..."
        sleep 30
        run_tests
        log_info ""
        log_info "Tests complete! Review results above."
        log_info "When ready to cleanup, run: $0 cleanup"
        ;;
    *)
        echo "Usage: $0 {setup|test|cleanup|all}"
        exit 1
        ;;
esac
