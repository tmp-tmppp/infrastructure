# NEXT STEP (disabled): adopt the hand-made resource group rso-rg and cluster rso-cluster.
#
# Values were read from Azure on 2026-11-04 (az aks show and az aks maintenanceconfiguration
# list, read-only). The commented code below validates with azurerm 5.12. To enable it, in its
# own pull request:
#
#  1. Start the cluster first: reading it fetches its user credential, which fails while it
#     is stopped (az aks show -g rso-rg -n rso-cluster --query powerState.code -o tsv must
#     print Running). Starting costs credit for every hour it runs.
#  2. Uncomment everything below this header.
#  3. Plan and read it line by line:
#       terraform -chdir=envs/prod plan -out=plan.tfplan
#     The goal is "2 to import, 0 to add, 0 to change, 0 to destroy". Every "~" line is a
#     value that differs from Azure: copy the real value here, never accept the change.
#     STOP on "must be replaced", "forces replacement", "-/+" or "destroy" for rso-rg or
#     rso-cluster: that would delete the cluster and everything running on it.
#     Check in particular: both maintenance windows (a block missing here would DELETE that
#     window in Azure), node_provisioning_profile (see ignore_changes), and that no
#     storage_profile or load_balancer_profile line appears.
#     auto_scaler_profile, advanced networking, the load balancer profile and the storage
#     profile are left to Azure (not written here). If the plan shows a "~" on one of them,
#     remove that block from the config, or add that exact attribute path to ignore_changes.
#     Do not accept the change.
#  4. Apply the reviewed plan (an import-only apply writes the state and changes nothing in
#     Azure); the next plan must say "No changes".
#     From then on every plan refreshes the cluster, and the provider reads its user credential
#     on each refresh. If that read fails while the cluster is stopped (step 1, to be confirmed
#     at the adoption), plan while the cluster runs, or use -refresh=false only for changes that
#     do not touch rso-rg or rso-cluster (such a plan does not detect drift).
#  5. Then, in a separate pull request, point azure.tf at the managed resources: replace
#     data.azurerm_resource_group.rso with azurerm_resource_group.rso and local.rso_cluster_id
#     with azurerm_kubernetes_cluster.rso.id, and delete the two data sources. That plan must
#     show no change; if it wants to replace the role assignment (a different spelling of the
#     same ID), keep the data sources.
#
# After the import the state holds the cluster credential (kube_config) in plain text:
# whoever can read the state container can administer the cluster.

# import {
#   to = azurerm_resource_group.rso
#   id = "/subscriptions/${var.subscription_id}/resourceGroups/rso-rg"
# }
#
# import {
#   to = azurerm_kubernetes_cluster.rso
#   id = "/subscriptions/${var.subscription_id}/resourceGroups/rso-rg/providers/Microsoft.ContainerService/managedClusters/rso-cluster"
# }
#
# resource "azurerm_resource_group" "rso" {
#   name     = "rso-rg"
#   location = "denmarkeast"
#
#   lifecycle {
#     prevent_destroy = true
#   }
# }
#
# resource "azurerm_kubernetes_cluster" "rso" {
#   name                = "rso-cluster"
#   location            = azurerm_resource_group.rso.location
#   resource_group_name = azurerm_resource_group.rso.name
#   dns_prefix          = "rso-cluster-dns"                  # a change forces a new cluster
#   node_resource_group = "MC_rso-rg_rso-cluster_italynorth" # Azure's value, written exactly; a change forces a new cluster
#   sku_tier            = "Free"
#   support_plan        = "KubernetesOfficial"
#
#   # Written out because azurerm 5.x defaults may differ from the hand-made cluster.
#   oidc_issuer_enabled               = true # true -> false would force a new cluster
#   workload_identity_enabled         = true
#   role_based_access_control_enabled = true # a change forces a new cluster
#   local_account_disabled            = false
#   run_command_enabled               = true
#   azure_policy_enabled              = false
#   cost_analysis_enabled             = false
#   image_cleaner_enabled             = true
#   image_cleaner_interval_hours      = 168
#   automatic_upgrade_channel         = "patch"
#   node_os_upgrade_channel           = "NodeImage"
#   # kubernetes_version stays out: the patch upgrade channel moves it, Azure reports it.
#
#   default_node_pool {
#     name                         = "agentpool" # renaming forces a new cluster
#     vm_size                      = "Standard_DS2_v2"
#     auto_scaling_enabled         = true
#     min_count                    = 2 # Q-29 (ADR-039): lower after the M1 measurements, as a reviewed user apply
#     max_count                    = 5 # Q-29 lowers it at the first start
#     node_count                   = 2 # the autoscaler owns it, see ignore_changes
#     os_disk_type                 = "Managed"
#     os_disk_size_gb              = 128 # a change rotates the nodes
#     max_pods                     = 110 # a change rotates the nodes
#     os_sku                       = "Ubuntu"
#     type                         = "VirtualMachineScaleSets"
#     kubelet_disk_type            = "OS"
#     scale_down_mode              = "Delete"
#     node_public_ip_enabled       = false
#     only_critical_addons_enabled = false
#     # temporary_name_for_rotation stays out while importing (Terraform-only; it would turn the
#     # import into an in-place update). Add it in the change that rotates the nodes.
#
#     upgrade_settings {
#       max_surge = "10%"
#     }
#   }
#
#   identity {
#     type = "SystemAssigned"
#   }
#
#   node_provisioning_profile {
#     mode = "Manual" # "Auto" (node auto-provisioning) clusters cannot be stopped and started
#   }
#
#   # Both upgrade windows exist in Azure (aksManagedAutoUpgradeSchedule and
#   # aksManagedNodeOSUpgradeSchedule: weekly, Sunday 00:00 UTC, 8 hours). The provider reads
#   # them into the state, so a block missing here would make the apply DELETE that window.
#   # start_date stays out: Azure sets it (Optional and Computed in the provider).
#   maintenance_window_auto_upgrade {
#     frequency   = "Weekly"
#     interval    = 1
#     day_of_week = "Sunday"
#     duration    = 8
#     start_time  = "00:00"
#     utc_offset  = "+00:00"
#   }
#
#   maintenance_window_node_os {
#     frequency   = "Weekly"
#     interval    = 1
#     day_of_week = "Sunday"
#     duration    = 8
#     start_time  = "00:00"
#     utc_offset  = "+00:00"
#   }
#
#   network_profile {
#     network_plugin      = "azure"
#     network_plugin_mode = "overlay"
#     network_data_plane  = "azure"
#     load_balancer_sku   = "standard"
#     outbound_type       = "loadBalancer"
#     pod_cidr            = "10.244.0.0/16"
#     service_cidr        = "10.0.0.0/16"
#     dns_service_ip      = "10.0.0.10"
#     ip_versions         = ["IPv4"]
#     # network_policy stays out: Azure reports "none" (NetworkPolicies are not enforced).
#     # Choosing an engine later reimages the nodes or forces a new cluster.
#     # load_balancer_profile stays out: Azure reports idle timeout and outbound ports as null,
#     # and the provider's defaults (30 minutes) would show as an in-place update. Azure's
#     # managed outbound IP count is already 1.
#   }
#
#   # storage_profile stays out: azurerm 5.12 never reads it back from Azure, so after the
#   # import it would always plan "+ storage_profile" (one in-place cluster update). Azure has
#   # the disk, file and snapshot drivers on and the blob driver off (az aks show).
#
#   lifecycle {
#     prevent_destroy = true
#     ignore_changes = [
#       default_node_pool[0].node_count,                 # the autoscaler changes the count
#       node_provisioning_profile[0].default_node_pools, # Azure reports null in Manual mode; the provider's default "Auto" would show as "~"
#     ]
#   }
# }
