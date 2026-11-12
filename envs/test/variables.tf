variable "subscription_id" {
  type        = string
  description = "Azure subscription ID of the project (an identifier, not a secret). Set it per session after az account set: export TF_VAR_subscription_id=\"$(az account show --query id -o tsv)\" (README.md, \"Every session\")."

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID; take it from az account show."
  }
}

variable "github_owner" {
  type        = string
  default     = "tmp-tmppp"
  description = "GitHub TEST organization managed by this configuration. The real organization belongs to envs/prod."

  validation {
    condition     = lower(var.github_owner) != "rso-ekipa10"
    error_message = "envs/test never manages RSO-ekipa10: that organization belongs to envs/prod (README.md, \"Test organization\")."
  }
}

variable "devs_members" {
  type        = list(string)
  description = "GitHub user names of the team devs in the test organization (both members). No default: export TF_VAR_devs_members='[\"<member-1>\",\"<member-2>\"]' per session."

  validation {
    condition     = length(var.devs_members) > 0 && alltrue([for name in var.devs_members : can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,38}$", name))])
    error_message = "devs_members must list at least one GitHub user name (letters, digits and hyphens, without the @)."
  }
}

variable "ruleset_enforcement" {
  type        = string
  default     = "active"
  description = "Enforcement of the default-branch rulesets of the service repositories: active (as in envs/prod) or disabled (the rulesets stay, nothing is enforced)."

  validation {
    condition     = contains(["active", "disabled"], var.ruleset_enforcement)
    error_message = "ruleset_enforcement must be active or disabled."
  }
}
