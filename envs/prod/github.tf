# GitHub organization settings as code.
#
# Repositories are created by hand by the team; Terraform never creates, deletes or archives
# one. Settings that already exist are adopted with import blocks. Credentials (DOCKERHUB_TOKEN,
# database passwords, API keys) are never managed here, because whatever Terraform manages ends
# up in its state. The two organization secrets below hold identifiers, not credentials: they
# are secrets only so that GitHub masks them in the public deploy logs (README.md, "Secrets").
#
# First plan: "3 to import, 13 to add, 2 to change, 0 to destroy" (README.md, "First apply").
# The only "~" lines allowed in it are listed above the two resources they belong to.

data "github_organization" "this" {
  name         = var.github_owner
  summary_only = true # the numeric ID is enough; skips listing members and repositories
}

data "github_repository" "deploy" {
  for_each = local.deploy_repositories

  full_name = "${var.github_owner}/${each.key}"
}

# ---------------------------------------------------------------------------------------------
# Values for azure/login, visible only to the repositories that deploy. The client ID is an
# organization variable (vars.AZURE_CLIENT_ID); the tenant and subscription IDs are
# organization secrets (secrets.AZURE_TENANT_ID, secrets.AZURE_SUBSCRIPTION_ID), read from
# Azure at plan time and never written in a file.

resource "github_actions_organization_variable" "azure_client_id" {
  variable_name           = "AZURE_CLIENT_ID"
  value                   = module.deploy_identity.client_id
  visibility              = "selected"
  selected_repository_ids = [for repo in data.github_repository.deploy : repo.repo_id]
}

locals {
  azure_login_secrets = {
    AZURE_TENANT_ID       = data.azurerm_client_config.current.tenant_id
    AZURE_SUBSCRIPTION_ID = data.azurerm_client_config.current.subscription_id
  }
}

resource "github_actions_organization_secret" "azure_login" {
  for_each = toset(keys(local.azure_login_secrets))

  secret_name = each.key
  value       = local.azure_login_secrets[each.key] # stored in the remote state (README.md, "Secrets")
  visibility  = "selected"                          # which repositories: the resource below
}

# Repository access as its own resource: selected_repository_ids on the secret is deprecated in
# provider 6.13.
resource "github_actions_organization_secret_repositories" "azure_login" {
  for_each = toset(keys(local.azure_login_secrets))

  secret_name             = github_actions_organization_secret.azure_login[each.key].secret_name
  selected_repository_ids = [for repo in data.github_repository.deploy : repo.repo_id]
}

# Docker Hub namespace for the image tags, read by every service pipeline as
# vars.DOCKERHUB_USERNAME. The access token stays the hand-made organization secret
# DOCKERHUB_TOKEN and is never managed here.
resource "github_actions_organization_variable" "dockerhub_username" {
  variable_name = "DOCKERHUB_USERNAME"
  value         = var.dockerhub_username
  visibility    = "all"
}

# If one of these variables was already created by hand, creating it fails with
# "409 Already exists". Then uncomment its block, so Terraform adopts it instead:
#
# import {
#   to = github_actions_organization_variable.azure_client_id
#   id = "AZURE_CLIENT_ID"
# }
# import {
#   to = github_actions_organization_variable.dockerhub_username
#   id = "DOCKERHUB_USERNAME"
# }
#
# Organization secrets need no import: creating one that already exists overwrites it.
# Delete by hand, once the pipelines read the new names: a hand-made organization VARIABLE
# AZURE_TENANT_ID or AZURE_SUBSCRIPTION_ID, and the hand-made organization SECRET
# DOCKERHUB_USERNAME (different objects; Terraform does not manage them).

# ---------------------------------------------------------------------------------------------
# Write access for the team devs (both members). A CODEOWNERS entry counts only for a user
# with write access, so without this the code-owner rule of the ruleset could never be met.
# The .github repository itself is not managed here, only the team's access to it.

data "github_team" "devs" {
  slug         = "devs"
  summary_only = true # the ID is enough; skips listing members and repositories
}

resource "github_team_repository" "devs" {
  for_each = toset(["markets", ".github"]) # add each new service repository here

  team_id    = data.github_team.devs.id
  repository = each.key
  permission = "push"
}

# ---------------------------------------------------------------------------------------------
# Repository markets (created by hand on 2026-11-04). Every value below is the one GitHub
# reports today, except security_and_analysis, which this configuration turns on. Allowed "~"
# lines in the first plan: archive_on_destroy and ignore_vulnerability_alerts_during_read
# (Terraform-only values, nothing changes on GitHub) and security_and_analysis (secret scanning
# and push protection, disabled -> enabled). Any other "~" line is drift: copy GitHub's value
# here instead of accepting it.

import {
  to = github_repository.markets
  id = "markets"
}

resource "github_repository" "markets" {
  #checkov:skip=CKV_GIT_1: public on purpose (graded code; rulesets and environments need public repositories on GitHub Free)
  name       = "markets"
  visibility = "public"

  has_issues      = true
  has_projects    = true
  has_wiki        = true
  has_discussions = false

  allow_merge_commit     = true
  allow_squash_merge     = true
  allow_rebase_merge     = true
  allow_auto_merge       = false
  allow_update_branch    = false
  delete_branch_on_merge = false

  merge_commit_title          = "MERGE_MESSAGE"
  merge_commit_message        = "PR_TITLE"
  squash_merge_commit_title   = "COMMIT_OR_PR_TITLE"
  squash_merge_commit_message = "COMMIT_MESSAGES"

  # Free on public repositories: GitHub alerts on leaked tokens and rejects pushes that contain
  # one, also for web edits and for clones without the local hooks. advanced_security stays out
  # (GitHub rejects it on public repositories). GitHub returns these settings only to a token
  # with admin on the repository; with less, every plan shows a diff here.
  security_and_analysis {
    secret_scanning {
      status = "enabled"
    }
    secret_scanning_push_protection {
      status = "enabled"
    }
  }

  # Second safety net: should prevent_destroy ever be removed, a destroy archives the
  # repository (reversible by hand) instead of deleting it. To stop managing the repository,
  # use a removed block with destroy = false, never terraform destroy.
  archive_on_destroy = true

  lifecycle {
    prevent_destroy = true
  }
}

# Pull requests from Dependabot for dependencies with a known vulnerability (the vulnerability
# alerts are already on). They go through the same ruleset as every other pull request.
resource "github_repository_dependabot_security_updates" "markets" {
  repository = github_repository.markets.name
  enabled    = true
}

# Environment production of markets (created by hand): deploys only from main, no reviewers,
# so every change on main deploys automatically. Its deployment history is evidence, so it is
# never deleted. Allowed "~" line in the first plan: prevent_self_review (GitHub reports it
# only when reviewers exist; the same value is sent back).

import {
  to = github_repository_environment.markets_production
  id = "markets:production"
}

resource "github_repository_environment" "markets_production" {
  repository          = github_repository.markets.name
  environment         = "production"
  can_admins_bypass   = true
  prevent_self_review = false

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = github_repository_environment_deployment_policy.markets_production_main
  id = "markets:production:65465026" # repository:environment:policy ID (gh api repos/RSO-ekipa10/markets/environments/production/deployment-branch-policies)
}

resource "github_repository_environment_deployment_policy" "markets_production_main" {
  repository     = github_repository.markets.name
  environment    = github_repository_environment.markets_production.environment
  branch_pattern = "main"
}

# Ruleset on the default branch of markets (new; rulesets are free on public repositories).
# Active from the first apply: changes reach main only through a pull request whose check
# "pipeline / test", reported by GitHub Actions, passed. So the first commit of markets is
# pushed before the first apply, and from then on every pull request must pass the pipeline
# (README.md, "First apply"). CODEOWNERS of markets lists .github/ and helm/, so a code-owner
# review is needed only for pipeline and chart changes; everything else merges without a
# review. No bypass actors.

resource "github_repository_ruleset" "markets_default_branch" {
  name        = "protect-default-branch"
  repository  = github_repository.markets.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  rules {
    deletion         = true # the branch cannot be deleted
    non_fast_forward = true # no force pushes

    pull_request {
      required_approving_review_count   = 0
      require_code_owner_review         = true
      dismiss_stale_reviews_on_push     = true # a new push after an approval of a pipeline or chart change needs a new approval
      require_last_push_approval        = false
      required_review_thread_resolution = false
    }

    required_status_checks {
      strict_required_status_checks_policy = false
      do_not_enforce_on_create             = true

      required_check {
        context        = "pipeline / test" # job "test" of the reusable pipeline, called by the job "pipeline"
        integration_id = 15368             # the GitHub Actions app (gh api apps/github-actions --jq .id); no other source satisfies it
      }
    }
  }

  # The second code owner gets write access before the code-owner rule applies.
  depends_on = [github_team_repository.devs]
}
