# VPC, subnets and egress.
#
# Public subnets carry the NAT gateways and any load balancer. Everything that
# runs the platform sits in private subnets: the nodes, the database and the
# stream consumers. Nothing that holds telemetry is directly reachable.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

variable "name" { type = string }
variable "vpc_cidr" { type = string }
variable "availability_zones" { type = list(string) }
variable "single_nat_gateway" { type = bool }

locals {
  az_count = length(var.availability_zones)

  # /20 public and /20 private per zone out of a /16. Roughly 4000 addresses per
  # private subnet, which matters on EKS: the VPC CNI assigns a real VPC address
  # to every pod, so subnet sizing caps pod density rather than node count.
  public_subnets  = [for i in range(local.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnets = [for i in range(local.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_subnets[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.name}-public-${var.availability_zones[count.index]}"
    # EKS looks for these tags to decide where to place a public load balancer.
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = local.private_subnets[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = {
    Name                              = "${var.name}-private-${var.availability_zones[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

# --- Egress ----------------------------------------------------------------

resource "aws_eip" "nat" {
  count  = var.single_nat_gateway ? 1 : local.az_count
  domain = "vpc"
  tags   = { Name = "${var.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count = var.single_nat_gateway ? 1 : local.az_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags       = { Name = "${var.name}-nat-${count.index}" }
  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One private route table per zone even when sharing a single NAT, so that
# switching single_nat_gateway to false later is a route change rather than a
# rebuild of every association.
resource "aws_route_table" "private" {
  count = local.az_count

  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
  }

  tags = { Name = "${var.name}-private-${var.availability_zones[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# --- VPC endpoints ---------------------------------------------------------
#
# S3 and Kinesis are the two services this platform talks to constantly. Without
# endpoints every byte of archive and every stream read leaves through the NAT
# gateway and is charged per gigabyte. The S3 gateway endpoint is free; the
# Kinesis interface endpoint costs an hourly rate per zone and pays for itself
# well below the volume this platform runs at. See docs/cost_model.md.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = { Name = "${var.name}-s3" }
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-vpc-endpoints"
  description = "Allow HTTPS from inside the VPC to interface endpoints"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from within the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Return traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-vpc-endpoints" }
}

resource "aws_vpc_endpoint" "kinesis" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.kinesis-streams"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.name}-kinesis" }
}

data "aws_region" "current" {}

# --- Outputs ---------------------------------------------------------------

output "vpc_id" { value = aws_vpc.this.id }
output "vpc_cidr" { value = aws_vpc.this.cidr_block }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "nat_gateway_count" { value = length(aws_nat_gateway.this) }
