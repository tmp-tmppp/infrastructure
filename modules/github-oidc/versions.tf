terraform {
  required_version = ">= 1.17, < 2.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 5.0, < 6.0" # a module takes a range; the root configuration pins the minor version
    }
  }
}
