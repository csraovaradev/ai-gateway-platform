output "bedrock_role_arn" {
  description = "Role Vault assumes to issue Bedrock credentials (aws/roles/bedrock role_arns)."
  value       = aws_iam_role.bedrock.arn
}

output "vault_user_name" {
  description = "IAM user whose access key is stored in Vault's aws/config/root."
  value       = aws_iam_user.vault.name
}
