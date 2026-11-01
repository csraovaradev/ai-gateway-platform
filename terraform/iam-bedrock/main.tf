# IAM for Phase 2 (ADR-0001): Vault issues short-lived STS credentials for the
# aigw-bedrock role. Nothing here creates an access key: Vault's key is created
# out of band and immediately rotated by Vault, so it never lands in tfstate.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "ai-gateway-platform", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# Identity Vault uses to call sts:AssumeRole. Local dev only; on EKS Vault uses IRSA instead.
resource "aws_iam_user" "vault" {
  name = "${var.name_prefix}-vault-dev"
  path = "/aigw/"
}

data "aws_iam_policy_document" "vault_user" {
  statement {
    sid       = "AssumeBedrockRole"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.bedrock.arn]
  }

  # Lets `vault write aws/config/rotate-root` replace this user's own key.
  statement {
    sid       = "RotateOwnKey"
    actions   = ["iam:GetUser", "iam:CreateAccessKey", "iam:DeleteAccessKey", "iam:ListAccessKeys"]
    resources = [aws_iam_user.vault.arn]
  }
}

resource "aws_iam_user_policy" "vault" {
  name   = "assume-bedrock-and-rotate-own-key"
  user   = aws_iam_user.vault.name
  policy = data.aws_iam_policy_document.vault_user.json
}

# Role the gateway effectively runs as when calling Bedrock.
data "aws_iam_policy_document" "bedrock_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "AWS"
      # The dev Vault's IAM user, plus Vault's IRSA role on EKS (from terraform/eks).
      identifiers = concat([aws_iam_user.vault.arn], var.vault_role_arns)
    }
  }
}

resource "aws_iam_role" "bedrock" {
  name                 = "${var.name_prefix}-bedrock"
  path                 = "/aigw/"
  assume_role_policy   = data.aws_iam_policy_document.bedrock_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid     = "InvokeModels"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    # Cross-region inference profiles (us.*, global.*) route to foundation models in other regions.
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "invoke-models-only"
  role   = aws_iam_role.bedrock.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}
