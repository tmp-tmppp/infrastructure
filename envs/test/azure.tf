# Azure part of the TEST organization: a second deploy identity, next to the one of envs/prod.
#
# rso-rg and rso-cluster are only READ here (data sources). They are never imported into this
# configuration: one Azure object is managed by at most one state, and their adoption belongs to
# envs/prod (envs/prod/imports.tf). The identity below lives in rso-rg, but it is its own object
# with its own name, so the two states never manage the same thing.

locals {
  # The same repositories as envs/prod, in the test organization => the GitHub environment of the
  # deploy job. Every repository listed must exist in the test organization before the plan
  # (create_test_repos.sh creates them with their environment production limited to main).
  # audit-export has no AKS deploy job (an Azure Function, deployed separately), so it is not here.
  deploy_repositories = {
    markets          = "production"
    trading          = "production"
    market-data      = "production"
    market-maker     = "production"
    broker           = "production"
    observability    = "production"
    tenants          = "production"
    payments         = "production"
    audit            = "production"
    payment-provider = "production"
    identity         = "production"
    gateway          = "production"
    database         = "production"
    web              = "production"
  }
}

# Guard against planning in the wrong subscription, as in envs/prod: only the project
# subscription holds the state storage account. Reads names only. Needs Reader on rso-tfstate-rg.
data "azurerm_client_config" "current" {
  lifecycle {
    postcondition {
      condition     = length(data.azurerm_resources.state_account.resources) == 1
      error_message = "Wrong subscription: the state storage account rsotfstate1318 is not in rso-tfstate-rg here. Run az account set --subscription <project subscription>, then export TF_VAR_subscription_id again (README.md, \"Every session\")."
    }
  }
}

data "azurerm_resources" "state_account" {
  resource_group_name = "rso-tfstate-rg"
  type                = "Microsoft.Storage/storageAccounts"
  name                = "rsotfstate1318"
}

data "azurerm_resource_group" "rso" {
  name = "rso-rg"
}

# The cluster's resource ID through the generic resource list: no cluster credential in this
# state, and it works while the cluster is stopped.
data "azurerm_resources" "rso_cluster" {
  resource_group_name = data.azurerm_resource_group.rso.name
  type                = "Microsoft.ContainerService/managedClusters"
  name                = "rso-cluster"

  lifecycle {
    postcondition {
      condition     = length(self.resources) == 1
      error_message = "Expected exactly one AKS cluster named rso-cluster in rso-rg."
    }
  }
}

locals {
  rso_cluster_id = one(data.azurerm_resources.rso_cluster.resources[*].id)
}

module "deploy_identity" {
  source = "../../modules/github-oidc"

  # Not rso-github-deploy: that one belongs to envs/prod and trusts only RSO-ekipa10.
  identity_name       = "rso-github-deploy-test"
  resource_group_name = data.azurerm_resource_group.rso.name
  location            = data.azurerm_resource_group.rso.location
  github_owner        = var.github_owner
  github_owner_id     = tonumber(data.github_organization.this.id)

  repositories = {
    for name, environment in local.deploy_repositories : name => {
      repository_id = data.github_repository.deploy[name].repo_id
      environment   = environment
    }
  }

  role_assignments = {
    # The same role as the identity of envs/prod: enough to fetch a kubeconfig and deploy with
    # Helm. Without Entra ID integration and with local accounts on, it is effectively cluster
    # admin (team decision C, 2026-11-04). Both organizations deploy into the same cluster, so
    # only one of them may run deploy jobs at a time (README.md, "Test organization").
    aks_cluster_user = {
      scope = local.rso_cluster_id
      role  = "Azure Kubernetes Service Cluster User Role"
    }
  }

  tags = {
    project    = "chance"
    managed-by = "terraform"
    github-org = var.github_owner
  }
}
