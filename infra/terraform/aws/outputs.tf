# What you need to actually use the stack once it exists.

output "region" {
  description = "Region everything was created in."
  value       = var.region
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "nat_gateway_count" {
  description = "One per zone unless single_nat_gateway is set. Each costs about 35 USD a month."
  value       = module.network.nat_gateway_count
}

output "iot_endpoint" {
  description = "MQTT endpoint devices connect to."
  value       = data.aws_iot_endpoint.data.endpoint_address
}

output "iot_device_policy" {
  description = "Attach this policy to every device certificate."
  value       = module.iot.device_policy_name
}

output "iot_thing_type" {
  value = module.iot.thing_type_name
}

output "kinesis_stream_name" {
  value = module.streaming.stream_name
}

output "kinesis_dead_letter_stream_name" {
  value = module.streaming.dead_letter_stream_name
}

output "archive_bucket" {
  value = module.storage.archive_bucket_name
}

output "ecr_repository_urls" {
  description = "Push service images here."
  value       = module.storage.ecr_repository_urls
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "kubeconfig_command" {
  description = "Run this to point kubectl at the cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "workload_role_arn" {
  description = <<-EOT
    Annotate the telemetry-platform service account with this ARN so the pods can
    read Kinesis and write the archive without static credentials:

      eks.amazonaws.com/role-arn: <this value>
  EOT
  value       = module.eks.workload_role_arn
}

data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}
