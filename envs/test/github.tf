# GitHub settings of the TEST organization (var.github_owner, tmp-tmppp), as code.
#
# The same settings as envs/prod writes into RSO-ekipa10, adapted to repositories that
# create_test_repos.sh creates (README.md, "Test organization"). Terraform never creates, deletes
# or archives a repository. Owned elsewhere, never here:
#   - the environment production and its branch policy main of each repository: the script
#     creates them and keeps them on every run;
#   - the organization variables DOCKERHUB_USERNAME and PUBLIC_HOST and the secrets ACME_EMAIL,
#     DOCKERHUB_TOKEN and <SERVICE>_ENV: set by hand with gh (the script prints the commands);
#     credentials never go into a state.
#
# First plan: "14 to import, 83 to add, 14 to change, 0 to destroy" with two members in
# devs_members (README.md, "Test organization"). The only "~" lines allowed in it are listed
# above github_repository.service.

data "github_organization" "this" {
  name         = var.github_owner
  summary_only = true # the numeric ID is enough; skips listing members and repositories
}

data "github_repository" "deploy" {
  for_each = local.deploy_repositories

  full_name = "${var.github_owner}/${each.key}"
}

# ---------------------------------------------------------------------------------------------
# Values for azure/login, visible only to the repositories that deploy: the same names as in
# envs/prod, so the shared pipeline needs no change. The client ID is the one of the test
# identity; tenant and subscription are the same as for envs/prod.

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

resource "github_actions_organization_secret_repositories" "azure_login" {
  for_each = toset(keys(local.azure_login_secrets))

  secret_name             = github_actions_organization_secret.azure_login[each.key].secret_name
  selected_repository_ids = [for repo in data.github_repository.deploy : repo.repo_id]
}

# If AZURE_CLIENT_ID was already created by hand in the test organization, creating it fails
# with "409 Already exists". Then uncomment this block, so Terraform adopts it instead:
#
# import {
#   to = github_actions_organization_variable.azure_client_id
#   id = "AZURE_CLIENT_ID"
# }

# ---------------------------------------------------------------------------------------------
# Team devs. A new organization has no teams, so here Terraform CREATES the team (in RSO-ekipa10
# it was made by hand and envs/prod only reads it). Both members are maintainers: GitHub makes
# the creator of a team its maintainer anyway, so the role never flips, and either member can
# manage the team. A member who is not yet in the organization gets an invitation and stays
# "pending" until accepting it.

resource "github_team" "devs" {
  name        = "devs"
  description = "Chance team: write access to the project repositories"
  privacy     = "closed" # visible to organization members (GitHub's default for a new team)
}

resource "github_team_membership" "devs" {
  for_each = toset(var.devs_members)

  team_id  = github_team.devs.id
  username = each.key
  role     = "maintainer"
}

locals {
  # The repositories of envs/prod's list, plus infrastructure: create_test_repos.sh creates it in
  # the test organization (in RSO-ekipa10 it does not exist yet).
  team_repositories = setunion(keys(local.deploy_repositories), [".github", "audit-export", "infrastructure"])
}

# Write access, so that both members count as code owners (CODEOWNERS lists them by user name).
resource "github_team_repository" "devs" {
  for_each = local.team_repositories

  team_id    = github_team.devs.id
  repository = each.key
  permission = "push"
}

# ---------------------------------------------------------------------------------------------
# The service repositories (created by create_test_repos.sh), adopted like markets in envs/prod
# but all fourteen in one block: the script gives every one of them the same settings. Every
# value below is what that script produces (GitHub's defaults, wiki off, its description),
# except security_and_analysis, which this configuration turns on.
#
# Allowed "~" lines in the first plan, one per repository: archive_on_destroy and
# ignore_vulnerability_alerts_during_read (Terraform-only values, nothing changes on GitHub)
# and security_and_analysis (secret scanning and push protection turned on, if they are off).
# Any other "~" line is drift: copy GitHub's value here instead of accepting it.

import {
  for_each = local.deploy_repositories

  to = github_repository.service[each.key]
  id = each.key
}

resource "github_repository" "service" {
  #checkov:skip=CKV_GIT_1: public on purpose (graded code; rulesets and environments need public repositories on GitHub Free)
  for_each = local.deploy_repositories

  name        = each.key
  description = "Chance: ${each.key} (test organization)" # exactly the text create_test_repos.sh sets
  visibility  = "public"

  has_issues      = true
  has_projects    = true
  has_wiki        = false # the script creates the repositories with --disable-wiki
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

  # Free on public repositories; returned only to a token with admin on the repository.
  security_and_analysis {
    secret_scanning {
      status = "enabled"
    }
    secret_scanning_push_protection {
      status = "enabled"
    }
  }

  # Second safety net, as in envs/prod: a destroy would archive, not delete. To stop managing
  # these repositories, use a removed block with destroy = false (README.md, "Test organization",
  # moving to RSO-ekipa10), never terraform destroy with this block in place.
  archive_on_destroy = true

  lifecycle {
    prevent_destroy = true
  }
}

# Dependabot alerts, which the security updates below need. GitHub turns them on by default;
# in a new organization that is not checked, so it is set here (an idempotent switch).
resource "github_repository_vulnerability_alerts" "service" {
  for_each = local.deploy_repositories

  repository = github_repository.service[each.key].name
  enabled    = true
}

resource "github_repository_dependabot_security_updates" "service" {
  for_each = local.deploy_repositories

  repository = github_repository.service[each.key].name
  enabled    = true

  depends_on = [github_repository_vulnerability_alerts.service]
}

# The ruleset of envs/prod's markets, on the default branch of every service repository. With
# ruleset_enforcement = "active" (the default), changes reach main only through a pull request
# whose check "pipeline / test", reported by GitHub Actions, passed; so the script pushes main
# before the first apply. Code-owner review for the paths in each CODEOWNERS file. No bypass
# actors.

resource "github_repository_ruleset" "service_default_branch" {
  for_each = local.deploy_repositories

  name        = "protect-default-branch"
  repository  = github_repository.service[each.key].name
  target      = "branch"
  enforcement = var.ruleset_enforcement

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
      dismiss_stale_reviews_on_push     = true
      require_last_push_approval        = false
      required_review_thread_resolution = false
    }

    required_status_checks {
      strict_required_status_checks_policy = false
      do_not_enforce_on_create             = true

      required_check {
        context        = "pipeline / test" # job "test" of the reusable pipeline, called by the job "pipeline"
        integration_id = 15368             # the GitHub Actions app (gh api apps/github-actions --jq .id)
      }
    }
  }

  # The second code owner gets write access before the code-owner rule applies.
  depends_on = [github_team_repository.devs, github_team_membership.devs]
}
