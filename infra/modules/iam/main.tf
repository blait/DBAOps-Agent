############################################
# IAM module — 공통 실행 역할 베이스
############################################
# Phase 1: MCP Lambda 공통 실행 역할만 정의.
# Phase 2~: AgentCore Runtime 역할, ECS task 역할 추가.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp_lambda_base" {
  name               = "dbaops-${var.environment}-mcp-lambda-base"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "mcp_lambda_basic" {
  role       = aws_iam_role.mcp_lambda_base.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "mcp_lambda_vpc" {
  role       = aws_iam_role.mcp_lambda_base.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}
