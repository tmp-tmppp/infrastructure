# Azure part: the identity that the deploy jobs of the service repositories log in as.
#
# rso-rg and rso-cluster were created by hand and are only READ here (data sources).
# Adopting them into Terraform is the next step: imports.tf.

locals {
  # Repositories whose deploy job logs in to Azure => the GitHub environment of that job.
  # Add one line per service repository when it gets a deploy job (see README.md).
  deploy_repositories = {
    markets = "production"
  }
}

# Guard against planning in the wrong subscription: both members' subscriptions are named
# "Azure for Students", and both hold an rso-rg with an rso-cluster. Only the project
# subscription holds the state storage account (its name is globally unique; the same name as
# in backend.hcl). The guard sits on the client config because its tenant and subscription IDs
# are written into the GitHub secrets. The lookup reads names only: the azurerm_storage_account
# data source would put the account keys into the state. Needs Reader on rso-tfstate-rg
# (README.md, "Prerequisites").
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

# The cluster's resource ID through the generic resource list. The azurerm_kubernetes_cluster
# data source would also fetch the cluster credential (kube_config) into the state, and it
# needs a running cluster; this lookup needs neither.
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

  identity_name       = "rso-github-deploy"
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
    # Enough to fetch a kubeconfig and deploy with Helm. The cluster has no Entra ID
    # integration and local accounts are on, so this role returns the cluster's
    # admin-level kubeconfig: it is effectively cluster admin. Accepted for now (team
    # decision C, 2026-11-04) and revisited after milestone M2; real least privilege needs
    # Entra ID integration with Azure RBAC for Kubernetes.
    aks_cluster_user = {
      scope = local.rso_cluster_id
      role  = "Azure Kubernetes Service Cluster User Role"
    }
  }

  tags = {
    project    = "chance"
    managed-by = "terraform"
  }
}
