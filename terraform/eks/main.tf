# Phase 0: VPC and EKS for the gateway, Vault, and their dependencies.
# Cost while running is roughly $0.20/h (EKS control plane, one NAT gateway, three Spot Graviton
# nodes, the smallest ElastiCache node), plus about $0.03/h for the ALB and WAF when a domain is set.
# `terraform destroy` removes everything except the Route 53 domain itself.

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "ai-gateway-platform", ManagedBy = "terraform" }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = var.name
  cidr = var.vpc_cidr
  azs  = local.azs

  private_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 48 + i)]

  # One NAT gateway keeps cost down; use one per AZ for production availability.
  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29"

  name               = var.name
  kubernetes_version = var.kubernetes_version
  vpc_id             = module.vpc.vpc_id
  subnet_ids         = module.vpc.private_subnets

  # Public API endpoint, but only reachable from the admin's address.
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.admin_cidrs
  endpoint_private_access      = true

  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  addons = {
    vpc-cni = {
      before_compute = true
      # Enforce Kubernetes NetworkPolicies (the chart ships one; ADR-0005).
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    coredns    = {}
    kube-proxy = {}
    aws-ebs-csi-driver = {
      service_account_role_arn = aws_iam_role.ebs_csi.arn
      configuration_values     = jsonencode({ defaultStorageClass = { enabled = true } })
    }
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = var.node_instance_types
      capacity_type  = "SPOT"
      min_size       = var.node_count
      max_size       = var.node_count + 1
      desired_size   = var.node_count
      disk_size      = 20
    }
  }
}

# IRSA: lets a Kubernetes service account assume an IAM role with a projected token.
data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
