output "vpc_id" {
  value = module.network.vpc_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "public_subnet_ids" {
  value = module.network.public_subnet_ids
}

output "logs_bucket" {
  value = module.s3_logs.bucket_name
}

output "prometheus_endpoint" {
  value = module.ec2_prometheus.prometheus_endpoint
}

output "aurora_endpoint" {
  value = module.aurora_postgres.endpoint
}

output "aurora_secret_arn" {
  value = module.aurora_postgres.master_user_secret_arn
}

output "aurora_writer_resource_id" {
  value = module.aurora_postgres.writer_resource_id
}

output "ecr_repository_url" {
  value = module.agentcore.ecr_repository_url
}

output "cognito_user_pool_id" {
  value = module.agentcore.cognito_user_pool_id
}

output "cognito_app_client_id" {
  value = module.agentcore.cognito_app_client_id
}

output "agentcore_runtime_role_arn" {
  value = module.agentcore.runtime_role_arn
}

output "agentcore_gateway_role_arn" {
  value = module.agentcore.gateway_role_arn
}

output "prometheus_query_lambda_arn" {
  value = module.lambda_prometheus_query.function_arn
}

output "mysql_endpoint" {
  value = module.rds_mysql.endpoint
}

output "mysql_secret_arn" {
  value = module.rds_mysql.master_user_secret_arn
}

output "mysql_resource_id" {
  value = module.rds_mysql.resource_id
}

output "msk_cluster_arn" {
  value = module.msk_serverless.cluster_arn
}

output "msk_cluster_name" {
  value = module.msk_serverless.cluster_name
}

output "msk_bootstrap_brokers" {
  value = data.aws_msk_bootstrap_brokers.this.bootstrap_brokers_sasl_iam
}

output "ecs_cluster_name" {
  value = module.ecs_generators.cluster_name
}

output "data_gen_repo_url" {
  value = module.ecs_generators.data_gen_repo_url
}

output "log_gen_repo_url" {
  value = module.ecs_generators.log_gen_repo_url
}
