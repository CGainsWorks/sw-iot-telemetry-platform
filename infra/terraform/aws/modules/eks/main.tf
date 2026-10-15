# EKS cluster and a managed node group.
#
# Written directly against the AWS provider rather than through the community
# terraform-aws-eks module. That module is excellent and would be the right
# choice for a real deployment; here the point is to show what the pieces are,
# and a module call with forty inputs shows none of them.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    # Used to read the cluster's OIDC issuer certificate, whose thumbprint the
    # IAM identity provider needs.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

variable "name" { type = string }
variable "kubernetes_version" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "instance_types" { type = list(string) }
variable "capacity_type" { type = string }
variable "min_size" { type = number }
variable "max_size" { type = number }
variable "desired_size" { type = number }
variable "workload_policy_arns" {
  description = "IAM policies bound to the platform's Kubernetes service account through IRSA."
  type        = list(string)
  default     = []
}

data "aws_partition" "current" {}

# --- Control plane ---------------------------------------------------------

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  name = "/aws/eks/${var.name}/cluster"
  # Control plane logs are charged by ingestion and storage and are only ever
  # read while investigating something recent.
  retention_in_days = 30
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids = var.subnet_ids
    # Private-only endpoint would need a bastion or VPN to run kubectl. Public
    # with a CIDR allowlist is the usual compromise; the allowlist is left at
    # the default here because this has never been applied and an allowlist of
    # someone else's office address would be worse than none.
    endpoint_private_access = true
    endpoint_public_access  = true
  }

  # api and audit are the two that matter for an incident: what was asked for,
  # and who asked. The others are high volume and rarely read.
  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
  ]

  tags = { Name = var.name }
}

# --- OIDC provider for IRSA ------------------------------------------------

data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
}

# --- Node group ------------------------------------------------------------

data "aws_iam_policy_document" "node_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryReadOnly",
    # Lets the nodes be managed by Systems Manager, which removes the need for
    # SSH keys and a bastion.
    "AmazonSSMManagedInstanceCore",
  ])

  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/${each.value}"
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name}-default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  instance_types = var.instance_types
  capacity_type  = var.capacity_type
  # ARM64. The platform is Python and nginx, both of which have first-class
  # arm64 images, and Graviton is around 20 per cent cheaper for the same work.
  # The CI build produces multi-architecture images for this reason.
  ami_type = "AL2023_ARM_64_STANDARD"

  scaling_config {
    min_size     = var.min_size
    max_size     = var.max_size
    desired_size = var.desired_size
  }

  update_config {
    max_unavailable = 1
  }

  lifecycle {
    # The cluster autoscaler owns desired_size once it is running. Without this
    # every plan would try to reset it to the value in the configuration.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]

  tags = { Name = "${var.name}-default" }
}

# --- Add-ons ---------------------------------------------------------------

resource "aws_eks_addon" "core" {
  for_each = toset(["coredns", "kube-proxy", "vpc-cni", "aws-ebs-csi-driver"])

  cluster_name = aws_eks_cluster.this.name
  addon_name   = each.value

  # PRESERVE keeps a hand-applied configuration change rather than reverting it,
  # which is the safer default when something has been patched during an
  # incident and not yet folded back into this file.
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  depends_on = [aws_eks_node_group.this]
}

# --- Workload identity -----------------------------------------------------

data "aws_iam_policy_document" "workload_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }

    # Scoped to one namespace and one service account. Without both conditions
    # any pod in the cluster could assume this role.
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.this.url, "https://", "")}:sub"
      values   = ["system:serviceaccount:telemetry:telemetry-platform"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_iam_openid_connect_provider.this.url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "workload" {
  name               = "${var.name}-workload"
  assume_role_policy = data.aws_iam_policy_document.workload_assume.json
}

resource "aws_iam_role_policy_attachment" "workload" {
  for_each = toset(var.workload_policy_arns)

  role       = aws_iam_role.workload.name
  policy_arn = each.value
}

# --- Outputs ---------------------------------------------------------------

output "cluster_name" { value = aws_eks_cluster.this.name }
output "cluster_endpoint" { value = aws_eks_cluster.this.endpoint }
output "cluster_certificate_authority" {
  value     = aws_eks_cluster.this.certificate_authority[0].data
  sensitive = true
}
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.this.arn }
output "workload_role_arn" {
  description = "Annotate the telemetry-platform service account with this."
  value       = aws_iam_role.workload.arn
}
