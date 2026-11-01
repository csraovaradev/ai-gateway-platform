resource "aws_ecr_repository" "gateway" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # images are tagged with the git commit
  force_delete         = true        # so `terraform destroy` also removes pushed images

  image_scanning_configuration {
    scan_on_push = true
  }
}
