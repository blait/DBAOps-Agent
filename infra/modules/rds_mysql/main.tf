############################################
# RDS MySQL module — Phase 2
############################################
# MySQL 8.0, db.t4g.micro, slow + general → CWLogs.
# manage_master_user_password 로 Secrets Manager 자동 관리.

resource "aws_db_subnet_group" "this" {
  name       = "dbaops-${var.environment}-mysql"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "dbaops-${var.environment}-mysql" }
}

resource "aws_security_group" "this" {
  name_prefix = "dbaops-${var.environment}-mysql-"
  vpc_id      = var.vpc_id
  description = "RDS MySQL"

  ingress {
    description = "MySQL from VPC"
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-mysql" }
}

resource "aws_db_parameter_group" "this" {
  name        = "dbaops-${var.environment}-mysql80"
  family      = "mysql8.0"
  description = "DBAOps MySQL params"

  parameter {
    name  = "slow_query_log"
    value = "1"
  }
  parameter {
    name  = "long_query_time"
    value = "1"
  }
  parameter {
    name  = "general_log"
    value = "1"
  }
  parameter {
    name  = "log_output"
    value = "FILE"
  }
}

resource "aws_db_instance" "this" {
  identifier              = "dbaops-${var.environment}-mysql"
  engine                  = "mysql"
  engine_version          = var.engine_version
  instance_class          = var.instance_class
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_subnet_group_name    = aws_db_subnet_group.this.name
  vpc_security_group_ids  = [aws_security_group.this.id]
  parameter_group_name    = aws_db_parameter_group.this.name
  db_name                 = var.database_name
  username                = var.master_username
  manage_master_user_password = true
  backup_retention_period = 1
  skip_final_snapshot     = true
  apply_immediately       = true
  # t4g.micro 는 PI 미지원. db_subgraph 는 PG 의 PI 만 사용한다.
  performance_insights_enabled = false
  enabled_cloudwatch_logs_exports = ["error", "slowquery", "general"]
  publicly_accessible     = false
  deletion_protection     = false
}
