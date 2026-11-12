# State of the TEST organization: the same storage account and container as envs/prod, its own
# key (test.terraform.tfstate), so this configuration never reads or locks the state of envs/prod.
# Partial configuration: the names come from backend.hcl (copy backend.hcl.example).
#   terraform -chdir=envs/test init -backend-config=backend.hcl
# Login with Entra ID only (az login locally); no storage account keys or SAS tokens.
terraform {
  backend "azurerm" {
    use_azuread_auth = true
  }
}
