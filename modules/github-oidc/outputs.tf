output "client_id" {
  value       = azurerm_user_assigned_identity.this.client_id
  description = "Client ID for azure/login (an identifier, not a secret)."
}

output "principal_id" {
  value       = azurerm_user_assigned_identity.this.principal_id
  description = "Object ID of the identity, used in role assignments."
}

output "tenant_id" {
  value       = azurerm_user_assigned_identity.this.tenant_id
  description = "Entra ID tenant of the identity."
}

output "subjects" {
  value       = local.subjects
  description = "Expected OIDC subject per repository; compare with the subject claim in the azure/login log."
}
