############################################
# DBAOps-Agent — PoC 환경 (서울 단일 리전)
############################################
# Phase 1 진행 중: network / iam / s3_logs / aurora_postgres / ec2_prometheus / agentcore
# 나머지 모듈은 Phase 2~3에서 활성화한다.

module "network" {
  source = "../../modules/network"

  environment = var.environment
  vpc_cidr    = var.vpc_cidr
  azs         = var.azs
}

module "iam" {
  source = "../../modules/iam"

  environment = var.environment
}

module "s3_logs" {
  source = "../../modules/s3_logs"

  environment = var.environment
}

# Phase 1 자원 — 점진적으로 enable
# module "ec2_prometheus" {
#   source = "../../modules/ec2_prometheus"
#   ...
# }

# module "aurora_postgres" {
#   source = "../../modules/aurora_postgres"
#   ...
# }

# module "agentcore" {
#   source = "../../modules/agentcore"
#   ...
# }
