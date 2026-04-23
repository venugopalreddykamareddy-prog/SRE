output "aws_integration_id" {
  description = "The ID of the AWS integration"
  value       = datadog_integration_aws.example.id
}

output "datadog_api_key" {
  description = "The Datadog API key"
  value       = var.datadog_api_key
  sensitive   = true
}