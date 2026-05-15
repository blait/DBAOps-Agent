terraform {
  required_version = ">= 1.7.0"

  backend "s3" {
    # bootstrap.sh 가 생성하는 버킷/락 테이블을 가리키도록 채워 넣는다
    # bucket         = "dbaops-tfstate-<account_id>-ap-northeast-2"
    # key            = "envs/poc/terraform.tfstate"
    # region         = "ap-northeast-2"
    # dynamodb_table = "dbaops-tfstate-lock"
    # encrypt        = true
  }
}
