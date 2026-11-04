# Unit tests: a mock provider and `command = plan` only, so nothing is created in Azure
# and no credentials are needed. Run: terraform init -backend=false && terraform test

mock_provider "azurerm" {}

variables {
  identity_name       = "test-identity"
  resource_group_name = "test-rg"
  location            = "denmarkeast"
  github_owner        = "RSO-ekipa10"
  github_owner_id     = 111
  repositories = {
    markets = {
      repository_id = 222
      environment   = "production"
    }
  }
}

run "subject_is_id_based_with_environment" {
  command = plan

  assert {
    condition     = azurerm_federated_identity_credential.repo["markets"].subject == "repo:RSO-ekipa10@111/markets@222:environment:production"
    error_message = "The subject must be the immutable, ID-based form with the environment."
  }

  assert {
    condition     = azurerm_federated_identity_credential.repo["markets"].issuer == "https://token.actions.githubusercontent.com"
    error_message = "The issuer must be GitHub Actions."
  }

  assert {
    condition     = azurerm_federated_identity_credential.repo["markets"].name == "github-markets-production"
    error_message = "The credential name must contain the repository and the environment."
  }
}

run "no_roles_unless_requested" {
  command = plan

  assert {
    condition     = length(azurerm_role_assignment.this) == 0
    error_message = "The module must not assign roles that were not requested."
  }
}

run "rejects_more_than_20_repositories" {
  command = plan

  variables {
    repositories = {
      for i in range(21) : "repo${i}" => {
        repository_id = i + 1
        environment   = "production"
      }
    }
  }

  expect_failures = [var.repositories]
}

run "rejects_an_empty_environment" {
  command = plan

  variables {
    repositories = {
      markets = {
        repository_id = 222
        environment   = ""
      }
    }
  }

  expect_failures = [var.repositories]
}
