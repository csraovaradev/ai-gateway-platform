# Audit log storage that cannot be rewritten (ADR-0013): Fluent Bit ships the gateway's audit lines
# to S3 with Object Lock, so a record cannot be edited or deleted during the retention period.
#
# GOVERNANCE mode lets an account admin with s3:BypassGovernanceRetention remove records (and
# `terraform destroy` empty the bucket). COMPLIANCE mode would stop everyone, root included, for
# the whole retention period: the right choice for a real audit trail, and a trap for a demo.

resource "aws_s3_bucket" "audit" {
  bucket_prefix       = "${var.name}-audit-"
  object_lock_enabled = true
  force_destroy       = true # demo teardown; bypasses GOVERNANCE retention only
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.audit_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    id     = "expire-after-a-year"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}

data "aws_iam_policy_document" "audit_bucket" {
  statement {
    sid       = "TLSOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket     = aws_s3_bucket.audit.id
  policy     = data.aws_iam_policy_document.audit_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.audit]
}

# Fluent Bit may only add objects under audit/: it cannot read or delete them, and with versioning
# and Object Lock a new write never replaces an earlier record.
data "aws_iam_policy_document" "fluent_bit" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/audit/*"]
  }
}

resource "aws_iam_role_policy" "fluent_bit" {
  name   = "write-audit-only"
  role   = aws_iam_role.irsa["fluent-bit"].id
  policy = data.aws_iam_policy_document.fluent_bit.json
}
