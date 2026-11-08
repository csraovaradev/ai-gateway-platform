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

output "audit_bucket" {
  value = aws_s3_bucket.audit.bucket
}

output "fluent_bit_role_arn" {
  value = aws_iam_role.irsa["fluent-bit"].arn
}

# Public edge (ADR-0013): empty strings when var.domain_name is not set.
output "domain_name" {
  value = var.domain_name
}

output "api_host" {
  value = local.edge ? local.api_host : ""
}

output "sso_host" {
  value = local.edge ? local.sso_host : ""
}

output "certificate_arn" {
  value = local.edge ? aws_acm_certificate_validation.edge[0].certificate_arn : ""
}

output "waf_acl_arn" {
  value = local.edge ? aws_wafv2_web_acl.edge[0].arn : ""
}

output "lb_controller_role_arn" {
  value = local.edge ? aws_iam_role.irsa["lb-controller"].arn : ""
}

output "external_dns_role_arn" {
  value = local.edge ? aws_iam_role.irsa["external-dns"].arn : ""
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "keycloak_repository_url" {
  value = aws_ecr_repository.keycloak.repository_url
}

# The two public subnets (where the ALB's network interfaces live) as one range, for
# NetworkPolicies that admit the load balancer but not every pod in the VPC.
output "alb_cidr" {
  value = cidrsubnet(var.vpc_cidr, 7, 24)
}
