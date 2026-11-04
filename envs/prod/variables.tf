variable "subscription_id" {
  type        = string
  description = "Azure subscription ID of the project (an identifier, not a secret). Set it per session after az account set: export TF_VAR_subscription_id=\"$(az account show --query id -o tsv)\" (README.md, \"Every session\")."

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID; take it from az account show."
  }
}

variable "dockerhub_username" {
  type        = string
  description = "Docker Hub namespace the pipelines push images to (not a secret). No default: pass it per session, export TF_VAR_dockerhub_username=<namespace>."

  validation {
    condition     = can(regex("^[a-z0-9]{4,30}$", var.dockerhub_username))
    error_message = "dockerhub_username must be a Docker Hub namespace: 4 to 30 lowercase letters and digits, without a slash."
  }
}

variable "github_owner" {
  type        = string
  default     = "RSO-ekipa10"
  description = "GitHub organization managed by this configuration."
}
