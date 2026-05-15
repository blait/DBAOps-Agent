variable "region" {
  description = "AWS region (서울 단일 리전)"
  type        = string
  default     = "ap-northeast-2"
}

variable "environment" {
  description = "환경 식별자"
  type        = string
  default     = "poc"
}

variable "vpc_cidr" {
  description = "PoC VPC CIDR"
  type        = string
  default     = "10.40.0.0/16"
}

variable "azs" {
  description = "사용할 가용영역 (2개)"
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]
}

variable "bedrock_model_id" {
  description = "Bedrock 모델 ID"
  type        = string
  default     = "claude-opus-4-7"
}
