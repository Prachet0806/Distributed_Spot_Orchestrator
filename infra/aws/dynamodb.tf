# V2 control-plane state (ADR: persistence topology).
# Job Registry is authoritative for lifecycle/epoch/version/active-migration.

resource "aws_dynamodb_table" "job_registry" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "state"
    type = "S"
  }

  global_secondary_index {
    name            = "StateIndex"
    hash_key        = "state"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = "spot-arbitrage-registry"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "candidate_pools" {
  name         = "spot_arbitrage_candidate_pools"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "pool_id"

  attribute {
    name = "pool_id"
    type = "S"
  }

  tags = {
    Name        = "spot-arbitrage-pools"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "plans" {
  name         = "spot_arbitrage_plans"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "plan_id"

  attribute {
    name = "plan_id"
    type = "S"
  }

  tags = {
    Name        = "spot-arbitrage-plans"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "event_ledger" {
  name         = "spot_arbitrage_event_ledger"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "event_id"

  range_key = "consumer_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "consumer_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name        = "spot-arbitrage-events"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "checkpoints" {
  name         = "spot_arbitrage_checkpoints"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "checkpoint_id"

  attribute {
    name = "checkpoint_id"
    type = "S"
  }

  attribute {
    name = "lineage_id"
    type = "S"
  }

  global_secondary_index {
    name            = "LineageIndex"
    hash_key        = "lineage_id"
    projection_type = "ALL"
  }

  tags = {
    Name        = "spot-arbitrage-checkpoints"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "audit" {
  name         = "spot_arbitrage_audit"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "audit_id"

  attribute {
    name = "audit_id"
    type = "S"
  }

  attribute {
    name = "job_id"
    type = "S"
  }

  global_secondary_index {
    name            = "JobIndex"
    hash_key        = "job_id"
    projection_type = "ALL"
  }

  tags = {
    Name        = "spot-arbitrage-audit"
    Environment = var.environment
  }
}

# Track B2: control-plane self-fencing lease (15 s heartbeat / 45 s TTL).
# Single writer (min=max=1 + lease, ADR-020); loss of the lease halts
# actuation instead of risking split-brain coordination.
resource "aws_dynamodb_table" "control_plane_lease" {
  name         = "spot_arbitrage_control_plane_lease"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "lease_id"

  attribute {
    name = "lease_id"
    type = "S"
  }

  tags = {
    Name        = "spot-arbitrage-lease"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "migration_history" {
  name         = "spot_arbitrage_migration_history"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "migration_id"

  attribute {
    name = "migration_id"
    type = "S"
  }

  attribute {
    name = "job_id"
    type = "S"
  }

  global_secondary_index {
    name            = "JobIndex"
    hash_key        = "job_id"
    projection_type = "ALL"
  }

  tags = {
    Name        = "spot-arbitrage-history"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "operations" {
  name         = "spot_arbitrage_operations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "operation_key"

  attribute {
    name = "operation_key"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = "spot-arbitrage-operations"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "pool_reservations" {
  name         = "spot_arbitrage_pool_reservations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pool_id"

  attribute {
    name = "pool_id"
    type = "S"
  }

  tags = {
    Name        = "spot-arbitrage-pool-reservations"
    Environment = var.environment
  }
}

resource "aws_dynamodb_table" "config" {
  name         = "spot_arbitrage_config"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "config_id"

  attribute {
    name = "config_id"
    type = "S"
  }

  tags = {
    Name        = "spot-arbitrage-config"
    Environment = var.environment
  }
}
