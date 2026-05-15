output "vpc_id" {
  value       = module.network.vpc_id
  description = "PoC VPC ID"
}

output "private_subnet_ids" {
  value       = module.network.private_subnet_ids
  description = "Private subnet IDs"
}

output "public_subnet_ids" {
  value       = module.network.public_subnet_ids
  description = "Public subnet IDs"
}

output "logs_bucket" {
  value       = module.s3_logs.bucket_name
  description = "Log bucket name"
}
