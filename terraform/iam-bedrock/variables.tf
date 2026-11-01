variable "region" {
  description = "AWS region for the provider. IAM is global; this only affects API endpoints."
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix for IAM resource names."
  type        = string
  default     = "aigw"
}
