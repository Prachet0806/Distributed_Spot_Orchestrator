resource "aws_security_group" "spot_worker_sg" {
  name        = "spot-worker-sg-phase1"
  description = "Allow SSH from Orchestrator and internal communication"

  # 1. SSH from YOUR IP (Orchestrator)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip] # You will pass this in variables
  }

  # 2. Allow outbound internet (for pip install, S3 upload)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "spot-worker-sg"
  }
}