terraform {
  required_version = ">= 1.17, < 2.0"

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
