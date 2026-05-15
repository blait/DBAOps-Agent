############################################
# Aurora PostgreSQL module — Phase 1
############################################
# PG 15, db.t4g.medium writer + reader, Performance Insights on,
# Secrets Manager 관리 패스워드 (manage_master_user_password=true).

resource "aws_db_subnet_group" "this" {
  name       = "dbaops-${var.environment}-aurora-pg"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "dbaops-${var.environment}-aurora-pg" }
}

resource "aws_security_group" "this" {
  name_prefix = "dbaops-${var.environment}-aurora-pg-"
  vpc_id      = var.vpc_id
  description = "Aurora PG"

  ingress {
    description = "Postgres from VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-aurora-pg" }
}

resource "aws_rds_cluster_parameter_group" "this" {
  name        = "dbaops-${var.environment}-aurora-pg15"
  family      = "aurora-postgresql15"
  description = "DBAOps Aurora PG cluster params"

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  parameter {
    name  = "log_lock_waits"
    value = "1"
  }
}

resource "aws_rds_cluster" "this" {
  cluster_identifier              = "dbaops-${var.environment}-aurora-pg"
  engine                          = "aurora-postgresql"
  engine_version                  = var.engine_version
  database_name                   = var.database_name
  master_username                 = var.master_username
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.this.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name
  storage_encrypted               = true
  backup_retention_period         = 1
  preferred_backup_window         = "16:00-17:00"
  skip_final_snapshot             = true
  apply_immediately               = true
  deletion_protection             = false
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

resource "aws_rds_cluster_instance" "writer" {
  identifier              = "dbaops-${var.environment}-aurora-pg-writer"
  cluster_identifier      = aws_rds_cluster.this.id
  engine                  = aws_rds_cluster.this.engine
  engine_version          = aws_rds_cluster.this.engine_version
  instance_class          = var.instance_class
  db_subnet_group_name    = aws_db_subnet_group.this.name
  performance_insights_enabled = true
  performance_insights_retention_period = 7
  apply_immediately       = true
}

resource "aws_rds_cluster_instance" "reader" {
  count                   = var.create_reader ? 1 : 0
  identifier              = "dbaops-${var.environment}-aurora-pg-reader"
  cluster_identifier      = aws_rds_cluster.this.id
  engine                  = aws_rds_cluster.this.engine
  engine_version          = aws_rds_cluster.this.engine_version
  instance_class          = var.instance_class
  db_subnet_group_name    = aws_db_subnet_group.this.name
  performance_insights_enabled = true
  performance_insights_retention_period = 7
  apply_immediately       = true
}
