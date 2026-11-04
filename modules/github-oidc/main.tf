# A user-assigned managed identity that GitHub Actions jobs log in as with OIDC:
# no client secret exists anywhere, every job gets a short-lived token.
# No Entra ID app registration is needed.

locals {
  # GitHub sends the immutable, ID-based subject for this organization's repositories:
  #   gh api repos/<owner>/<repo>/actions/oidc/customization/sub
  # returns "use_immutable_subject": true and the prefix "repo:<owner>@<ownerId>/<repo>@<repoId>".
  # A job with `environment: <env>` then sends "<prefix>:environment:<env>".
  # The ID form also stops a renamed or re-created repository with the same name from logging in.
  subjects = {
    for name, repo in var.repositories :
    name => "repo:${var.github_owner}@${var.github_owner_id}/${name}@${repo.repository_id}:environment:${repo.environment}"
  }
}

resource "azurerm_user_assigned_identity" "this" {
  name                = var.identity_name
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "repo" {
  for_each = var.repositories

  name                      = "github-${each.key}-${each.value.environment}"
  user_assigned_identity_id = azurerm_user_assigned_identity.this.id
  issuer                    = "https://token.actions.githubusercontent.com"
  audience                  = ["api://AzureADTokenExchange"]
  subject                   = local.subjects[each.key]
}

resource "azurerm_role_assignment" "this" {
  for_each = var.role_assignments

  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  principal_type       = "ServicePrincipal" # a new identity may not be replicated yet; the type skips that lookup
}
