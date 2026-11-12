terraform {
  required_version = ">= 1.17, < 2.0"

  # The same provider versions as envs/prod (keep both lock files in step).
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.12"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.13"
    }
  }
}
