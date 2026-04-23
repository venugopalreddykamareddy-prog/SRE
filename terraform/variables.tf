# Datadog Configuration Variables

variable "datadog_api_key" {
  description = "The API key for Datadog" 
  type        = string
}

variable "datadog_app_key" {
  description = "The Application key for Datadog"
  type        = string
}

variable "environment" {
  description = "Deployment Environment"
  type        = string
}

variable "aws_account_id" {
  description = "AWS Account ID"
  type        = string
}

variable "aws_region" {
  description = "AWS Region"
  type        = string
}