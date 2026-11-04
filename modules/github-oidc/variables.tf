variable "identity_name" {
  type        = string
  description = "Name of the user-assigned managed identity that the GitHub Actions jobs log in as."
}

variable "resource_group_name" {
  type        = string
  description = "Existing resource group that holds the identity."
}

variable "location" {
  type        = string
  description = "Azure region of the identity."
}

variable "github_owner" {
  type        = string
  description = "GitHub organization that owns the repositories, e.g. RSO-ekipa10."
}

variable "github_owner_id" {
  type        = number
  description = "Numeric ID of the GitHub organization (part of the immutable OIDC subject)."
}

variable "repositories" {
  type = map(object({
    repository_id = number
    environment   = string
  }))
  description = "Repository name => numeric repository ID and the GitHub environment of the job that logs in. One federated credential per entry."

  validation {
    condition     = length(var.repositories) <= 20
    error_message = "A user-assigned managed identity holds at most 20 federated identity credentials."
  }

  validation {
    condition     = alltrue([for name, repo in var.repositories : length(trimspace(repo.environment)) > 0 && repo.repository_id > 0])
    error_message = "Every repository needs a positive repository_id and a non-empty environment."
  }
}

variable "role_assignments" {
  type = map(object({
    scope = string
    role  = string
  }))
  default     = {}
  description = "Azure roles for the identity, keyed by a short label: scope (resource ID) and built-in role name."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags for the identity."
}
