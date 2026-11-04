# Azure: logs in with the Azure CLI session of the member who runs Terraform (az login).
# The subscription comes from TF_VAR_subscription_id, never from a file.
provider "azurerm" {
  features {}

  subscription_id = var.subscription_id
  use_cli         = true

  # azurerm 5.x registers no resource providers by itself; registering one is a deliberate,
  # separate step (az provider register), so it is never a side effect of a plan.
  resource_provider_registrations = "none"
}

# GitHub: reads the token from the GITHUB_TOKEN environment variable
# (a token with the admin:org and repo scopes of an organization owner; see README.md).
# Never put it in a file.
provider "github" {
  owner = var.github_owner
}
