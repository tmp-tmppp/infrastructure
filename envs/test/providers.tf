# Azure: logs in with the Azure CLI session of the member who runs Terraform (az login), in the
# same subscription as envs/prod. The subscription comes from TF_VAR_subscription_id.
provider "azurerm" {
  features {}

  subscription_id = var.subscription_id
  use_cli         = true

  # No resource provider is ever registered as a side effect of a plan (as in envs/prod).
  resource_provider_registrations = "none"
}

# GitHub: the TEST organization only (var.github_owner refuses RSO-ekipa10). Reads the token from
# GITHUB_TOKEN: an owner of the test organization, scopes admin:org and repo. Never in a file.
provider "github" {
  owner = var.github_owner
}
