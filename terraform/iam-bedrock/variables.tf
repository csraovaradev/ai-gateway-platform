variable "region" {
  description = "AWS region for the provider. IAM is global; this only affects API endpoints."
  type        = string
  default     = "us-west-2"
}

variable "vault_role_arns" {
  description = "Additional Vault roles allowed to assume aigw-bedrock, e.g. the EKS IRSA role."
  type        = list(string)
  default     = []
}

variable "name_prefix" {
  description = "Prefix for IAM resource names."
  type        = string
  default     = "aigw"
}
