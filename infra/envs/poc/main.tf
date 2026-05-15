############################################
# DBAOps-Agent — PoC 환경 (서울 단일 리전)
############################################

module "network" {
  source = "../../modules/network"

  environment         = var.environment
  vpc_cidr            = var.vpc_cidr
  azs                 = var.azs
  enable_nat_instance = true
  enable_s3_endpoint  = true
  interface_endpoints = [
    "secretsmanager",
    "bedrock-runtime",
    "bedrock-agentcore",
    "bedrock-agentcore-control"
  ]
}

module "iam" {
  source      = "../../modules/iam"
  environment = var.environment
}

module "s3_logs" {
  source      = "../../modules/s3_logs"
  environment = var.environment
}

module "ec2_prometheus" {
  source = "../../modules/ec2_prometheus"

  environment = var.environment
  vpc_id      = module.network.vpc_id
  vpc_cidr    = module.network.vpc_cidr
  subnet_id   = module.network.private_subnet_ids[0]
  use_spot    = true
}

module "aurora_postgres" {
  source = "../../modules/aurora_postgres"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
  create_reader      = true
}

module "rds_mysql" {
  source = "../../modules/rds_mysql"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
}

module "msk_serverless" {
  source = "../../modules/msk_serverless"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
}

module "agentcore" {
  source           = "../../modules/agentcore"
  environment      = var.environment
  region           = var.region
  bedrock_model_id = var.bedrock_model_id
}

############################################
# MSK bootstrap brokers — data plane API 호출 (apply 시 1회)
############################################

data "aws_msk_bootstrap_brokers" "this" {
  cluster_arn = module.msk_serverless.cluster_arn
}

module "ecs_generators" {
  source = "../../modules/ecs_generators"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids

  logs_bucket     = module.s3_logs.bucket_name
  logs_bucket_arn = module.s3_logs.bucket_arn

  pg_host          = replace(module.aurora_postgres.endpoint, "/:.*$/", "")
  pg_dbname        = module.aurora_postgres.database_name
  pg_secret_arn    = module.aurora_postgres.master_user_secret_arn
  mysql_host       = replace(module.rds_mysql.endpoint, "/:.*$/", "")
  mysql_dbname     = module.rds_mysql.database_name
  mysql_secret_arn = module.rds_mysql.master_user_secret_arn

  msk_bootstrap = data.aws_msk_bootstrap_brokers.this.bootstrap_brokers_sasl_iam
  kafka_topic   = "dbaops.orders"
}

############################################
# Phase 1 MCP Lambda — prometheus_query
############################################

module "lambda_prometheus_query" {
  source = "../../modules/lambda_mcp"

  environment = var.environment
  tool_name   = "prometheus-query"
  source_dir  = "${path.root}/../../../mcp_tools/prometheus_query"
  handler     = "handler.handler"
  timeout     = 30
  memory_size = 256
  vpc_id      = module.network.vpc_id
  subnet_ids  = module.network.private_subnet_ids
  role_arn    = module.iam.mcp_lambda_base_role_arn

  env_vars = {
    PROMETHEUS_URL = module.ec2_prometheus.prometheus_endpoint
  }

  extra_security_group_ids = []
}
