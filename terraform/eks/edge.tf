# Public HTTPS front door (ADR-0013): api.<domain> for the gateway and sso.<domain> for sign-in,
# behind one ALB with an ACM certificate and AWS WAF. Created only when var.domain_name is set; the
# Route 53 hosted zone must already exist (registering a domain in Route 53 creates it).
#
# The ALB itself is created by the AWS Load Balancer Controller from the Kubernetes Ingress, and
# ExternalDNS points the two names at it; both run with IRSA roles defined here.

locals {
  edge     = var.domain_name != ""
  api_host = "api.${var.domain_name}"
  sso_host = "sso.${var.domain_name}"

  # namespace:serviceaccount for every controller that gets an IRSA role in this file or audit.tf.
  irsa_service_accounts = merge(
    local.edge ? {
      lb-controller = "kube-system:aws-load-balancer-controller"
      external-dns  = "kube-system:external-dns"
    } : {},
    { fluent-bit = "logging:fluent-bit" },
  )
}

data "aws_iam_policy_document" "irsa_trust" {
  for_each = local.irsa_service_accounts
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:${each.value}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "irsa" {
  for_each           = local.irsa_service_accounts
  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust[each.key].json
}

data "aws_route53_zone" "edge" {
  count        = local.edge ? 1 : 0
  name         = var.domain_name
  private_zone = false
}

# --- TLS certificate ------------------------------------------------------------------------

resource "aws_acm_certificate" "edge" {
  count                     = local.edge ? 1 : 0
  domain_name               = local.api_host
  subject_alternative_names = [local.sso_host]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "acm_validation" {
  for_each = local.edge ? {
    for o in aws_acm_certificate.edge[0].domain_validation_options : o.domain_name => o
  } : {}
  zone_id         = data.aws_route53_zone.edge[0].zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "edge" {
  count                   = local.edge ? 1 : 0
  certificate_arn         = aws_acm_certificate.edge[0].arn
  validation_record_fqdns = [for r in aws_route53_record.acm_validation : r.fqdn]
}

# --- Controllers ----------------------------------------------------------------------------

# AWS's published policy for this controller version, unchanged (docs/install/iam_policy.json).
resource "aws_iam_policy" "lb_controller" {
  count  = local.edge ? 1 : 0
  name   = "${var.name}-lb-controller"
  policy = file("${path.module}/policies/aws-load-balancer-controller-v3.6.2.json")
}

resource "aws_iam_role_policy_attachment" "lb_controller" {
  count      = local.edge ? 1 : 0
  role       = aws_iam_role.irsa["lb-controller"].name
  policy_arn = aws_iam_policy.lb_controller[0].arn
}

# ExternalDNS may only change records in this one zone.
data "aws_iam_policy_document" "external_dns" {
  count = local.edge ? 1 : 0
  statement {
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = [data.aws_route53_zone.edge[0].arn]
  }
  statement {
    actions   = ["route53:ListHostedZones", "route53:ListResourceRecordSets", "route53:ListTagsForResources"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "external_dns" {
  count  = local.edge ? 1 : 0
  name   = "route53-one-zone"
  role   = aws_iam_role.irsa["external-dns"].id
  policy = data.aws_iam_policy_document.external_dns[0].json
}

# --- WAF ------------------------------------------------------------------------------------

# AWS managed rules inspect headers, paths, and query strings. Their *_BODY rules are switched to
# count-only: request bodies here are prompts full of code, file paths, and security discussion,
# which those rules would block as attacks. Prompt content is the gateway's job (ADR-0010).
locals {
  waf_body_rules = {
    AWSManagedRulesCommonRuleSet = [
      "SizeRestrictions_BODY", "CrossSiteScripting_BODY", "GenericLFI_BODY",
      "GenericRFI_BODY", "EC2MetaDataSSRF_BODY",
    ]
    AWSManagedRulesKnownBadInputsRuleSet  = ["Log4JRCE_BODY", "JavaDeserializationRCE_BODY"]
    AWSManagedRulesAmazonIpReputationList = []
  }
}

resource "aws_wafv2_web_acl" "edge" {
  count = local.edge ? 1 : 0
  name  = "${var.name}-edge"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit-per-ip"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-limit-per-ip"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = keys(local.waf_body_rules)
    content {
      name     = rule.value
      priority = rule.key + 1
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.value
          dynamic "rule_action_override" {
            for_each = local.waf_body_rules[rule.value]
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.value
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-edge"
    sampled_requests_enabled   = true
  }
}
