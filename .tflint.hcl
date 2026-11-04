config {
  # Lint the local module through the calls in envs/prod as well.
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

# Dependabot does not see the ruleset version: bump it by hand, monthly, together with the
# TFLint version in .github/workflows/terraform.yml (README.md, "Tool versions").
plugin "azurerm" {
  enabled = true
  version = "0.33.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}
