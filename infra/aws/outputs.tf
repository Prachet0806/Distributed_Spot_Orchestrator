output "instance_id" {
  description = "ID of the launched spot instance (null unless create_demo_worker)"
  value       = var.create_demo_worker ? aws_instance.spot_worker[0].id : null
}

output "instance_public_ip" {
  description = "Public IP address of the spot instance (null unless create_demo_worker)"
  value       = var.create_demo_worker ? aws_instance.spot_worker[0].public_ip : null
}

output "instance_private_ip" {
  description = "Private IP address of the spot instance (null unless create_demo_worker)"
  value       = var.create_demo_worker ? aws_instance.spot_worker[0].private_ip : null
}

output "instance_public_dns" {
  description = "Public DNS name of the spot instance (null unless create_demo_worker)"
  value       = var.create_demo_worker ? aws_instance.spot_worker[0].public_dns : null
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = var.create_demo_worker ? "ssh -i ~/.ssh/${var.ssh_key_name}.pem ${var.ssh_user}@${aws_instance.spot_worker[0].public_ip}" : null
}

output "security_group_id" {
  description = "ID of the security group"
  value       = aws_security_group.spot_worker_sg.id
}

output "iam_role_arn" {
  description = "ARN of the IAM role attached to the instance"
  value       = aws_iam_role.spot_worker_role.arn
}

output "checkpoint_kms_key_arn" {
  description = "KMS key ARN for checkpoint SSE-KMS (set CHECKPOINT_KMS_KEY_ARN in the controller)"
  value       = aws_kms_key.checkpoint_key.arn
}

output "dynamodb_registry_table" {
  description = "Job registry table name (PK job_id + StateIndex GSI)"
  value       = aws_dynamodb_table.job_registry.name
}

