output "deploy_identity_client_id" {
  value       = module.deploy_identity.client_id
  description = "Client ID of the deploy identity (also in the organization variable AZURE_CLIENT_ID)."
}

output "deploy_identity_principal_id" {
  value       = module.deploy_identity.principal_id
  description = "Object ID of the deploy identity."
}

output "deploy_oidc_subjects" {
  value       = module.deploy_identity.subjects
  description = "Expected OIDC subject per repository; compare with the subject claim line in the azure/login log."
}
