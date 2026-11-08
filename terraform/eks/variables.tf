variable "region" {
  description = "AWS region for the cluster and Bedrock calls."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Name for the cluster and its resources."
  type        = string
  default     = "aigw"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.37"
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint, e.g. [\"203.0.113.7/32\"]."
  type        = list(string)
}

variable "vpc_cidr" {
  description = "VPC address range."
  type        = string
  default     = "10.40.0.0/16"
}

variable "node_instance_types" {
  description = "Graviton instance types for the Spot node group; several types improve Spot availability."
  type        = list(string)
  default     = ["t4g.medium", "t4g.large"]
}

variable "bedrock_role_name" {
  description = "Role created by terraform/iam-bedrock that Vault assumes to issue Bedrock credentials."
  type        = string
  default     = "aigw-bedrock"
}

variable "node_count" {
  description = "Spot nodes. Three lets each Vault replica (Raft, ADR-0013) run on its own node."
  type        = number
  default     = 3
}

variable "domain_name" {
  description = "Public domain with a Route 53 hosted zone, e.g. \"secureaigate.com\". Empty: no public edge."
  type        = string
  default     = ""
}

variable "waf_rate_limit" {
  description = "Requests per client IP per 5 minutes before WAF blocks it."
  type        = number
  default     = 2000
}

variable "audit_retention_days" {
  description = "Days each audit object is locked against deletion (S3 Object Lock, GOVERNANCE mode)."
  type        = number
  default     = 30
}
