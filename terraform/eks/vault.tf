# Vault on EKS (ADR-0001): auto-unseal with KMS, and AWS credentials through IRSA, so no long-lived
# AWS key exists anywhere. Vault assumes the aigw-bedrock role to issue 15-minute Bedrock credentials.

data "aws_iam_role" "bedrock" {
  name = var.bedrock_role_name
}

resource "aws_kms_key" "vault_unseal" {
  description             = "${var.name} Vault auto-unseal"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "vault_unseal" {
  name          = "alias/${var.name}-vault-unseal"
  target_key_id = aws_kms_key.vault_unseal.key_id
}

data "aws_iam_policy_document" "vault_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:vault:vault"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "vault" {
  name               = "${var.name}-vault"
  assume_role_policy = data.aws_iam_policy_document.vault_trust.json
}

data "aws_iam_policy_document" "vault" {
  statement {
    sid       = "Unseal"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.vault_unseal.arn]
  }
  statement {
    sid       = "IssueBedrockCredentials"
    actions   = ["sts:AssumeRole"]
    resources = [data.aws_iam_role.bedrock.arn]
  }
}

resource "aws_iam_role_policy" "vault" {
  name   = "unseal-and-issue-bedrock-credentials"
  role   = aws_iam_role.vault.id
  policy = data.aws_iam_policy_document.vault.json
}
