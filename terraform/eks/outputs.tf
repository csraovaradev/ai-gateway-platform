output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.gateway.repository_url
}

output "vault_role_arn" {
  description = "IRSA role for the Vault service account; add it to terraform/iam-bedrock's trusted roles."
  value       = aws_iam_role.vault.arn
}

output "vault_kms_key_id" {
  value = aws_kms_key.vault_unseal.key_id
}

output "redis_url" {
  value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
}

output "vpc_cidr" {
  value = var.vpc_cidr
}

output "bedrock_role_arn" {
  value = data.aws_iam_role.bedrock.arn
}
