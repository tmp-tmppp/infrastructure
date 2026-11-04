# infrastructure

Infrastructure as code for **Chance**, written in Terraform. One root configuration
(`envs/prod`) manages two things:

1. **Azure**: the identity that the GitHub Actions deploy jobs log in as (OIDC, no stored
   secret) and its role on the AKS cluster.
2. **GitHub organization settings** (`RSO-ekipa10`): organization Actions variables and
   secrets for the Azure login, the team's write access to the repositories, and the settings,
   `production` environment and branch ruleset of the service repositories.
   This replaces clicking through the settings pages: every change is a reviewed pull request.

## What is managed, and what deliberately is not

| Managed here | Where |
|---|---|
| User-assigned managed identity `rso-github-deploy` in `rso-rg`, one federated credential per deploying repository | `modules/github-oidc`, `envs/prod/azure.tf` |
| Role "Azure Kubernetes Service Cluster User Role" on `rso-cluster` for that identity | `envs/prod/azure.tf` |
| Organization variables `AZURE_CLIENT_ID` (visible to the deploying repositories) and `DOCKERHUB_USERNAME` (visible to all repositories) | `envs/prod/github.tf` |
| Organization secrets `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` (identifiers, visible to the deploying repositories; see [Secrets](#secrets-and-identifiers)) | `envs/prod/github.tf` |
| Write access ("push") of the team `devs` (both members) to `.github` and `markets` | `envs/prod/github.tf` |
| Repository `markets`: its settings (adopted with an `import` block) with secret scanning and push protection on, Dependabot security updates, its `production` environment with the deployment branch policy `main` (adopted), a ruleset on its default branch | `envs/prod/github.tf` |

| Not managed here | Why |
|---|---|
| Resource group `rso-rg` and cluster `rso-cluster` | Created by hand; only read for now. Adopting them is the next step, prepared in `envs/prod/imports.tf`. |
| Starting and stopping the cluster | `az aks start` / `az aks stop`; Terraform has no power-state setting, so stopping causes no drift. |
| The state storage (`rso-tfstate-rg`) | Created once by hand (bootstrap below) and protected by a `CanNotDelete` lock, so nothing in `envs/prod` can delete its own state. |
| Creating, deleting or archiving repositories | The team creates repositories by hand. Adopted repositories have `prevent_destroy` (and `archive_on_destroy` as a second net). |
| The `.github` repository's own settings | Not adopted; only the team's access to it is managed. Its security settings are turned on by hand (see [First apply](#first-apply), step 8). |
| Credentials (`DOCKERHUB_TOKEN`, database passwords, ...) | See [Secrets](#secrets-and-identifiers). |
| Requiring two-factor authentication in the organization | The API cannot change it; turned on by hand. |
| Kubernetes objects | Helm, from each service's pipeline. |

The ruleset on `markets` is active from the first apply. It requires a pull request whose
check `pipeline / test` passed (reported by the GitHub Actions app; no other source can
satisfy it), blocks force pushes and deleting the default branch, and requires a code-owner
review. The CODEOWNERS file of `markets` lists `.github/` and `helm/`, so only pipeline and
chart changes need a review. A code owner counts only with write access, which the team
`devs` gives both members.

**From the first apply on, every pull request to `main` of `markets` needs a green
`pipeline / test`.** The first M1 pull request must therefore already pass: committed
lockfile, passing tests, a chart that lints.

## Layout

```
envs/prod/            the only root configuration (prod is the only environment in Azure)
  versions.tf         Terraform and provider versions
  backend.tf          remote state in Azure Storage (partial configuration)
  backend.hcl.example copy to backend.hcl and fill in the storage account name
  providers.tf        azurerm (Azure CLI login) and github (GITHUB_TOKEN)
  variables.tf        subscription_id, dockerhub_username, github_owner
  azure.tf            the deploy identity and its cluster role, the subscription guard
  github.tf           organization variables and secrets, team access, markets settings,
                      environment, ruleset
  imports.tf          next step, disabled: adopt rso-rg and rso-cluster
  outputs.tf          client ID, principal ID, expected OIDC subjects
modules/github-oidc/  reusable: identity + one federated credential per repository + roles
  tests/              terraform test with a mock provider (plan only, no Azure access)
.tflint.hcl           TFLint with the azurerm ruleset
```

## OIDC subject

The organization's repositories send the immutable, ID-based subject:

```
repo:RSO-ekipa10@<organization ID>/<repository>@<repository ID>:environment:<environment>
```

so a renamed or re-created repository with the same name cannot log in. Terraform reads both
IDs from GitHub itself. To check them by hand:

```bash
gh api orgs/RSO-ekipa10 --jq .id
gh api repos/RSO-ekipa10/markets --jq .id
gh api repos/RSO-ekipa10/markets/actions/oidc/customization/sub   # "use_immutable_subject": true
terraform -chdir=envs/prod output deploy_oidc_subjects             # after the first apply
```

If an `azure/login` step fails with `AADSTS700213`, compare the `subject claim` line in its log
with that output.

## Prerequisites

Terraform 1.17.x, Azure CLI, GitHub CLI.

Your Azure account needs Owner on the subscription, or at least on `rso-rg` (Contributor plus
User Access Administrator or Role Based Access Control Administrator also works). Terraform
creates a managed identity and its federated credentials (Contributor-level rights), plus a
role assignment on the cluster (role-assignment rights). User Access Administrator alone fails
at apply. To read and lock the state you also need Storage Blob Data Contributor on the state
account, and the subscription guard in `envs/prod/azure.tf` needs Reader on `rso-tfstate-rg`
(Owner on the subscription covers both; bootstrap below). The teammate must be a member, or an
invited guest, of the subscription's directory; a guest signs in with
`az login --tenant <directory-domain>`.

The GitHub part needs an organization owner's token: organization variables and secrets, team
access and the repository's security settings are owner or admin operations.

## One-time bootstrap: the state storage

Run once, by the subscription owner. Proposed account name: `rsotfstate1318` (3-24 lowercase
letters and digits, globally unique; if the check below prints `false`, choose another number
and use it in `backend.hcl` and `envs/prod/azure.tf`).

```bash
az provider register --namespace Microsoft.Storage --wait
az storage account check-name -n rsotfstate1318 --query nameAvailable    # must print true

az group create -n rso-tfstate-rg -l denmarkeast
az storage account create -n rsotfstate1318 -g rso-tfstate-rg -l denmarkeast \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
  --allow-blob-public-access false --allow-shared-key-access false
SA_ID=$(az storage account show -n rsotfstate1318 -g rso-tfstate-rg --query id -o tsv)

# Data access with Entra ID for each member, by object ID (no directory lookup, so it also works
# for guest accounts and restricted university accounts). Find the object ID in the portal:
# Microsoft Entra ID > Users > <member> > Object ID (or `az ad signed-in-user show --query id -o tsv`
# if that works for you).
az role assignment create --assignee-object-id <member-object-id> --assignee-principal-type User \
  --role "Storage Blob Data Contributor" --scope "$SA_ID"
# The teammate also needs the control-plane roles from Prerequisites (the subscription owner runs these):
az role assignment create --assignee-object-id <teammate-object-id> --assignee-principal-type User \
  --role Owner --scope "$(az group show -n rso-rg --query id -o tsv)"
az role assignment create --assignee-object-id <teammate-object-id> --assignee-principal-type User \
  --role Reader --scope "$(az group show -n rso-tfstate-rg --query id -o tsv)"
# The roles take a few minutes to become effective, then:
az storage container create --account-name rsotfstate1318 -n tfstate --auth-mode login

# Recovery net for the state: blob versions, 7-day blob and container soft delete.
az storage account blob-service-properties update --account-name rsotfstate1318 -g rso-tfstate-rg \
  --enable-versioning true --enable-delete-retention true --delete-retention-days 7 \
  --enable-container-delete-retention true --container-delete-retention-days 7

# Nobody can delete the state account or its group by mistake (free; blob writes and the
# state lock still work). CanNotDelete, never ReadOnly.
az lock create --name keep-tfstate --resource-group rso-tfstate-rg --lock-type CanNotDelete
```

If the team wants the teammate to plan only, use Reader on `rso-rg` instead of Owner. Granting
either role is an Azure change that the subscription owner decides on. The lock does not
protect the data itself (deleting a blob or the container is a data operation); the soft
delete settings cover that case. If the account already exists, run only the last two
commands.

Then, in each working copy:

```bash
cp envs/prod/backend.hcl.example envs/prod/backend.hcl    # backend.hcl is ignored by Git
# set storage_account_name = "rsotfstate1318"
```

No storage keys or SAS tokens exist: shared-key access is off, and the backend logs in with
Entra ID (`use_azuread_auth`). State locking is automatic (a blob lease).

## Every session

```bash
az login
az account list --query "[].{name:name, id:id}" -o table   # both members' subscriptions are named "Azure for Students"; pick the project one by ID (ask the owner)
az account set --subscription <project subscription ID>
az storage account show -n rsotfstate1318 -g rso-tfstate-rg --query name -o tsv   # must print rsotfstate1318; if not, this is the wrong subscription
export TF_VAR_subscription_id="$(az account show --query id -o tsv)"
export TF_VAR_dockerhub_username="<docker-hub-namespace>"

# The github provider needs admin:org (organization Actions variables and secrets, team access)
# and repo (gh always keeps repo):
gh auth refresh -h github.com -s admin:org
export GITHUB_TOKEN="$(gh auth token)"

terraform -chdir=envs/prod init -backend-config=backend.hcl
```

The token and the IDs live only in environment variables of the shell, never in a file.
Terraform checks the subscription itself as well: the plan stops with "Wrong subscription" when
`rsotfstate1318` is not in `rso-tfstate-rg` of the current subscription.

At the end of the session:

```bash
# Take admin:org back from the stored gh login.
unset GITHUB_TOKEN
gh auth refresh -h github.com -r admin:org
```

## First apply

In this order:

1. The pipeline side is ready: the shared workflow in `.github` (on `main`) and the caller in
   `markets` read `secrets.AZURE_TENANT_ID`, `secrets.AZURE_SUBSCRIPTION_ID`,
   `vars.AZURE_CLIENT_ID`, `vars.DOCKERHUB_USERNAME` and `secrets.DOCKERHUB_TOKEN`.
2. The state storage exists (bootstrap above).
3. Push the first commit of `markets` to `main` (at least `.github/workflows/ci-cd.yml`,
   `.github/CODEOWNERS`, `.github/dependabot.yml`) before the ruleset exists. After the apply
   `main` takes changes only through pull requests with a green `pipeline / test`.
4. Set up the session (above) and plan:
   `terraform -chdir=envs/prod plan -out=plan.tfplan`
5. The plan must say **`3 to import, 13 to add, 2 to change, 0 to destroy`**:
   - import: `github_repository.markets`, `github_repository_environment.markets_production`,
     `github_repository_environment_deployment_policy.markets_production_main`;
   - add, Azure: the identity, its federated credential for `markets`, its role on the cluster
     (`module.deploy_identity...`);
   - add, GitHub: the variables `AZURE_CLIENT_ID` and `DOCKERHUB_USERNAME`, the secrets
     `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` and their repository access (2 + 2), the
     team access to `markets` and `.github` (2), Dependabot security updates for `markets`,
     the ruleset;
   - change: `github_repository.markets` with exactly `archive_on_destroy` and
     `ignore_vulnerability_alerts_during_read` (Terraform-only values) and
     `security_and_analysis` (secret scanning and push protection turned on), and
     `github_repository_environment.markets_production` with exactly `prevent_self_review`
     (Terraform-only; the same value is sent back). Any other `~` line is drift: copy
     GitHub's value into `github.tf` instead of accepting it.
6. Review and apply as in the routine below.
7. Check the result (names and statuses only):
   ```bash
   gh api repos/RSO-ekipa10/markets/codeowners/errors              # expect "errors": []
   gh api repos/RSO-ekipa10/markets --jq .security_and_analysis    # secret scanning and push protection "enabled"
   gh api orgs/RSO-ekipa10/actions/secrets --jq '.secrets[].name'
   gh api repos/RSO-ekipa10/markets/rulesets --jq '.[] | [.name, .enforcement] | @tsv'
   ```
8. By hand (not in Terraform):
   - `.github` repository, Settings > Advanced Security: turn on secret scanning, push
     protection and Dependabot security updates (free on public repositories);
   - organization settings: require two-factor authentication;
   - delete hand-made leftovers once the pipelines read the new names: organization variables
     `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` and the organization secret
     `DOCKERHUB_USERNAME`, if they exist.

## Routine: plan, review, apply

1. Create a branch, change the code, run the same checks as CI:
   ```bash
   terraform fmt -recursive
   terraform -chdir=envs/prod validate
   terraform -chdir=modules/github-oidc test
   tflint --recursive
   ```
2. Plan and save it: `terraform -chdir=envs/prod plan -out=plan.tfplan`
3. Read the plan. Find `Plan: X to add, Y to change, Z to destroy` (and `N to import`).
   Stop if anything is destroyed or replaced (`-/+`, `must be replaced`) that you did not
   intend, and never apply a plan that replaces `rso-cluster`, `rso-rg`, the deploy identity or
   its role assignment, or that changes `AZURE_SUBSCRIPTION_ID` / `AZURE_TENANT_ID` (a `~` on
   `github_actions_organization_secret.azure_login`: the plan hides the value, so a change
   there means another subscription or tenant).
4. Open a pull request. Paste only the `Plan:` line and the list of changed resource addresses
   with their actions into its description, never attribute values. The plan shows IDs (role
   assignment scope, resource IDs) in plain text, and this repository is public:
   ```bash
   terraform -chdir=envs/prod show -no-color plan.tfplan | grep -E '^(  # |Plan:)'
   ```
   The other member reviews the values by planning the branch locally.
5. After the approval, apply exactly the reviewed plan, then merge right away so `main` matches
   what runs:
   `terraform -chdir=envs/prod apply plan.tfplan`
6. Delete `plan.tfplan` (it contains values in plain text; it is ignored by Git), and end the
   session (above).

If the plan says the state is locked, someone else is running Terraform: wait, never force
the unlock.

## Adding the next service repository

1. Create the repository by hand (public), with its `production` environment limited to `main`.
2. Push its first commit to `main` (at least `.github/workflows/ci-cd.yml` and
   `.github/CODEOWNERS`) before any ruleset exists for it.
3. Add one line to `local.deploy_repositories` in `envs/prod/azure.tf`:
   ```hcl
   deploy_repositories = {
     markets = "production"
     wallet  = "production"
   }
   ```
   The plan adds one federated credential and adds the repository to the visibility of
   `AZURE_CLIENT_ID` and of both `AZURE_*` secrets.
4. Add its name to `github_team_repository.devs` in `envs/prod/github.tf`, so both members can
   be its code owners.
5. Optionally adopt its settings, environment and ruleset like `markets` in `github.tf`
   (`import` blocks; the environment's policy ID is listed by
   `gh api repos/RSO-ekipa10/<repo>/environments/production/deployment-branch-policies`;
   copy the `#checkov:skip=CKV_GIT_1` line into its repository block). Its ruleset is active
   from its apply, so its pipeline must pass from then on.
6. After the apply: `gh api repos/RSO-ekipa10/<repo>/codeowners/errors` must show `"errors": []`.

One identity holds at most 20 federated credentials; the module checks that.

## Secrets and identifiers

Terraform writes every value it manages into the state file. So credentials stay out of
Terraform completely: the Docker Hub token is an organization secret set by hand
(`gh secret set`), and application secrets follow the same route (or, later, Azure Key Vault
with workload identity).

The tenant and subscription IDs are identifiers, not credentials: knowing them gives no access
without a federated credential that matches the repository's subject. They are organization
secrets only so that GitHub masks them in the logs of the public repositories. Their values
come from Azure at plan time and are never written in a file, but they do live in the remote
state: encrypted at rest by Azure Storage, readable only with Storage Blob Data Contributor on
the state account (the two members), and locked with a blob lease during every run. In a plan
they show as `(sensitive value)`. The client ID and the Docker Hub namespace are plain
organization variables.

The Azure login itself needs no secret at all: GitHub issues a short-lived OIDC token per job,
and Azure trusts it through the federated credential.

## Continuous integration

`.github/workflows/terraform.yml` runs on every pull request and on pushes to `main`:
`terraform fmt -check`, `init -backend=false` and `validate`, the module tests (mock provider),
TFLint with the azurerm ruleset, and Checkov (Terraform and workflow policies). The job has
read-only repository access and no cloud credentials. Actions are pinned to full commit SHAs;
Dependabot proposes updates of providers and actions weekly. Checkov's one expected finding,
`CKV_GIT_1` (the repository is public), is skipped inline on that repository with its reason;
there are no global skips.

There is no plan job yet. Plan-on-pull-request comes once two things exist: a separate,
read-only plan identity (Reader on `rso-rg` and `rso-tfstate-rg`, the cluster-user role,
Storage Blob Data Contributor on the state container, its own federated credential for this
repository) and a GitHub token for the github provider stored as a secret. Until then a member
plans locally and pastes the summary into the pull request.

### Tool versions

Dependabot updates the providers (the lock file) and the pinned actions. It does not see the
tool versions: Terraform (`TERRAFORM_VERSION` in `terraform.yml`, `required_version` in the
`versions.tf` files), TFLint (`TFLINT_VERSION`), the TFLint azurerm ruleset (`.tflint.hcl`)
and Checkov (`CHECKOV_VERSION`). Bump them by hand, monthly: look up the latest release
(`gh release view -R hashicorp/terraform`, `-R terraform-linters/tflint`,
`-R terraform-linters/tflint-ruleset-azurerm`, `-R bridgecrewio/checkov`), change the line,
and let the pull request's `terraform` check pass. Pull the matching local Docker images
(`hashicorp/terraform:<version>`, `ghcr.io/terraform-linters/tflint:<version>`) at the same
time.

## Costs

| Item | Cost |
|---|---|
| State storage account and container | well under USD 0.10 a month |
| Managed identity, federated credential, role assignments, resource lock | free |
| GitHub variables, secrets, team access, environment, ruleset, secret scanning, push protection, Dependabot security updates (public repositories, GitHub Free) | free |
| Running plan and apply | free; until the cluster is adopted, the plan reads only the cluster's ID, which works while it is stopped |

## Next steps

- Adopt `rso-rg` and `rso-cluster` (`envs/prod/imports.tf`, needs the cluster running).
  After the adoption every plan refreshes the cluster, and the provider then reads its user
  credential on each refresh. If that read fails while the cluster is stopped (imports.tf
  step 1, still to be confirmed at the first adoption), plan while the cluster runs anyway, or
  use `-refresh=false` only for changes that do not touch `rso-rg` or `rso-cluster`. Such a
  plan does not detect drift. After the adoption the state also holds the cluster credential,
  so only the two members may read it.
- A monthly budget alert on `rso-rg`.
- The read-only plan identity and plan-on-pull-request.
- Once this repository exists on GitHub: add it to `github_team_repository.devs` and give it a
  ruleset.
