# State in Azure Storage, locked with a blob lease, in its own resource group (rso-tfstate-rg),
# so deleting rso-rg never deletes the state.
# Partial configuration: the names come from backend.hcl (copy backend.hcl.example).
#   terraform -chdir=envs/prod init -backend-config=backend.hcl
# Login with Entra ID only (az login locally); no storage account keys or SAS tokens.
terraform {
  backend "azurerm" {
    use_azuread_auth = true
  }
}
