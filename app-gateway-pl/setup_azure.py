"""
Deploy all Azure infrastructure for the Application Gateway POC and write
a complete resource manifest to azure-resources.json.

Runs the full setup sequence as a single operation:
  1. Create resource group
  2. Deploy Bicep template (VNet, Public IP, Application Gateway, Private Link)
  3. Query each deployed resource for full details
  4. Add the Application Gateway public IP to the Aura BC allowlist
  5. Write azure-resources.json with every resource ID, name, and state

Usage:
    uv run python setup_azure.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "aurabc-appgw-poc-rg")
LOCATION = os.getenv("AZURE_LOCATION", "westus3")
DEPLOYMENT_NAME = "aurabc-appgw-poc"
BICEP_FILE = os.path.join(BASE_DIR, "infra", "main.bicep")
PARAMS_FILE = os.path.join(BASE_DIR, "infra", "parameters.json")
RESOURCES_FILE = os.path.join(BASE_DIR, "azure-resources.json")
PREFIX = os.getenv("AZURE_PREFIX", "aurabc-appgw")


def run_az(cmd, check=True, parse_json=True):
    """Run an az CLI command and return the result."""
    full_cmd = f"az {cmd}"
    if parse_json and "--output" not in cmd and "-o " not in cmd:
        full_cmd += " --output json"

    print(f"  > {full_cmd}")
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)

    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return None

    if parse_json and result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()

    return result.stdout.strip()


def check_az_cli():
    """Verify az CLI is installed and logged in."""
    result = subprocess.run(
        "az account show --output json",
        shell=True, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("ERROR: Not logged in to Azure CLI. Run 'az login' first.")
        sys.exit(1)
    account = json.loads(result.stdout)
    print(f"Azure subscription: {account['name']} ({account['id']})")
    return account


def load_params():
    """Load parameters from the JSON file and .env overrides."""
    with open(PARAMS_FILE) as f:
        params = json.load(f)["parameters"]

    aura_fqdn = os.getenv("NEO4J_URI", "")
    if aura_fqdn.startswith("neo4j+s://"):
        aura_fqdn = aura_fqdn.replace("neo4j+s://", "")
    elif aura_fqdn.startswith("bolt+s://"):
        aura_fqdn = aura_fqdn.replace("bolt+s://", "")

    if aura_fqdn and aura_fqdn != "REPLACE_WITH_AURA_FQDN":
        params["auraFqdn"]["value"] = aura_fqdn

    if params["auraFqdn"]["value"] == "REPLACE_WITH_AURA_FQDN":
        print("ERROR: Set NEO4J_URI in .env or auraFqdn in infra/parameters.json")
        sys.exit(1)

    return params


def save_resources(resources):
    """Write the resource manifest to disk."""
    with open(RESOURCES_FILE, "w") as f:
        json.dump(resources, f, indent=2)
    print(f"  Saved to {RESOURCES_FILE}")


def add_to_allowlist(ip_address):
    """Add the Application Gateway public IP to the Aura BC allowlist."""
    manage_script = os.path.join(BASE_DIR, "manage_ip_allowlist.py")
    result = subprocess.run(
        [sys.executable, manage_script, "add", "--ip", ip_address, "--description", "App Gateway POC"],
        cwd=BASE_DIR,
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print("WARNING: Failed to add IP to allowlist. Check Aura API credentials in .env")
        return None

    # Try to extract filter ID from output
    for line in result.stdout.splitlines():
        if line.startswith("Filter ID:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


# ---------------------------------------------------------------------------
# Main setup flow
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("APPLICATION GATEWAY POC — AZURE SETUP")
    print("=" * 60)

    account = check_az_cli()
    params = load_params()
    aura_fqdn = params["auraFqdn"]["value"]
    aura_port = params.get("auraPort", {}).get("value", 7687)

    resources = {
        "metadata": {
            "deployedAt": datetime.now(timezone.utc).isoformat(),
            "deploymentName": DEPLOYMENT_NAME,
            "subscriptionId": account["id"],
            "subscriptionName": account["name"],
            "location": LOCATION,
            "prefix": PREFIX,
            "auraFqdn": aura_fqdn,
            "auraPort": aura_port,
        },
    }

    print(f"\nAura BC FQDN: {aura_fqdn}")
    print(f"Resource Group: {RESOURCE_GROUP}")
    print(f"Location: {LOCATION}")

    # ------------------------------------------------------------------
    # Step 1: Resource group
    # ------------------------------------------------------------------
    print(f"\n[1/5] Creating resource group...")
    rg = run_az(f"group create --name {RESOURCE_GROUP} --location {LOCATION}")
    if rg is None:
        sys.exit(1)

    resources["resourceGroup"] = {
        "name": RESOURCE_GROUP,
        "location": LOCATION,
        "resourceId": rg.get("id", ""),
    }
    save_resources(resources)

    # ------------------------------------------------------------------
    # Step 2: Deploy Bicep template
    # ------------------------------------------------------------------
    print(f"\n[2/5] Deploying Bicep template (this takes 5-10 minutes for App Gateway)...")

    resolved_params_path = os.path.join(BASE_DIR, "infra", ".params-resolved.json")
    resolved = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": params,
    }
    with open(resolved_params_path, "w") as f:
        json.dump(resolved, f, indent=2)

    result = run_az(
        f'deployment group create '
        f'--name {DEPLOYMENT_NAME} '
        f'--resource-group {RESOURCE_GROUP} '
        f'--template-file {BICEP_FILE} '
        f"--parameters @{resolved_params_path}"
    )

    if os.path.exists(resolved_params_path):
        os.remove(resolved_params_path)

    if result is None:
        print("\nDeployment FAILED. Check errors above.")
        save_resources(resources)
        sys.exit(1)

    # Extract Bicep outputs
    bicep_outputs = {}
    raw_outputs = result.get("properties", {}).get("outputs", {})
    for key, val in raw_outputs.items():
        bicep_outputs[key] = val.get("value", "")

    resources["metadata"]["bicepOutputs"] = bicep_outputs
    save_resources(resources)

    # ------------------------------------------------------------------
    # Step 3: Query each resource for full details
    # ------------------------------------------------------------------
    print(f"\n[3/5] Querying deployed resources...")

    # Public IP
    print("  Querying public IP...")
    pip = run_az(
        f"network public-ip show --resource-group {RESOURCE_GROUP} --name {PREFIX}-pip",
        check=False,
    )
    if pip and isinstance(pip, dict):
        resources["publicIp"] = {
            "name": pip.get("name", ""),
            "resourceId": pip.get("id", ""),
            "ipAddress": pip.get("ipAddress", ""),
            "allocationMethod": pip.get("publicIPAllocationMethod", ""),
            "sku": pip.get("sku", {}).get("name", ""),
        }
    save_resources(resources)

    # VNet and subnets
    print("  Querying VNet...")
    vnet = run_az(
        f"network vnet show --resource-group {RESOURCE_GROUP} --name {PREFIX}-vnet",
        check=False,
    )
    if vnet and isinstance(vnet, dict):
        subnets = {}
        for subnet in vnet.get("subnets", []):
            subnet_name = subnet.get("name", "")
            delegations = [
                d.get("serviceName", "")
                for d in subnet.get("delegations", [])
            ]
            subnets[subnet_name] = {
                "name": subnet_name,
                "resourceId": subnet.get("id", ""),
                "addressPrefix": subnet.get("addressPrefix", ""),
                "delegations": delegations or None,
                "privateLinkServiceNetworkPolicies": subnet.get("privateLinkServiceNetworkPolicies", ""),
            }

        resources["vnet"] = {
            "name": vnet.get("name", ""),
            "resourceId": vnet.get("id", ""),
            "addressSpace": vnet.get("addressSpace", {}).get("addressPrefixes", []),
            "subnets": subnets,
        }
    save_resources(resources)

    # Application Gateway
    print("  Querying Application Gateway...")
    appgw = run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw",
        check=False,
    )
    if appgw and isinstance(appgw, dict):
        # Extract L4 TCP listener details
        listener_info = None
        for l in appgw.get("listener", appgw.get("listeners", [])):
            props = l.get("properties", l)
            listener_info = {
                "name": l.get("name", ""),
                "protocol": props.get("protocol", ""),
            }
            break

        # Extract L7 HTTP listener details (required for Private Link validation)
        http_listener_info = None
        for l in appgw.get("httpListeners", []):
            props = l.get("properties", l)
            http_listener_info = {
                "name": l.get("name", ""),
                "protocol": props.get("protocol", ""),
            }
            break

        # Extract backend pool details
        backend_pool_info = None
        for pool in appgw.get("backendAddressPools", []):
            props = pool.get("properties", pool)
            addresses = props.get("backendAddresses", [])
            backend_pool_info = {
                "name": pool.get("name", ""),
                "addresses": [
                    a.get("fqdn", a.get("ipAddress", ""))
                    for a in addresses
                ],
            }
            break

        # Extract backend setting details
        backend_setting_info = None
        for s in appgw.get("backendSettingsCollection", []):
            props = s.get("properties", s)
            backend_setting_info = {
                "name": s.get("name", ""),
                "protocol": props.get("protocol", ""),
                "port": props.get("port", ""),
                "timeout": props.get("timeout", ""),
            }
            break

        # Extract Private Link config
        pl_config_info = None
        for plc in appgw.get("privateLinkConfigurations", []):
            pl_config_info = {
                "name": plc.get("name", ""),
                "resourceId": plc.get("id", ""),
            }
            break

        # Extract private endpoint connections
        pe_connections = []
        for conn in appgw.get("privateEndpointConnections", []):
            props = conn.get("properties", conn)
            state = props.get("privateLinkServiceConnectionState", {})
            pe_connections.append({
                "name": conn.get("name", ""),
                "resourceId": conn.get("id", ""),
                "status": state.get("status", ""),
            })

        resources["applicationGateway"] = {
            "name": appgw.get("name", ""),
            "resourceId": appgw.get("id", ""),
            "provisioningState": appgw.get("provisioningState", ""),
            "operationalState": appgw.get("operationalState", ""),
            "sku": appgw.get("sku", {}).get("name", ""),
            "privateLinkConfigName": pl_config_info["name"] if pl_config_info else "",
            "listener": listener_info,
            "httpListener": http_listener_info,
            "backendPool": backend_pool_info,
            "backendSetting": backend_setting_info,
            "privateLinkConfig": pl_config_info,
            "privateEndpointConnections": pe_connections,
        }
    save_resources(resources)

    # ------------------------------------------------------------------
    # Step 4: Backend health
    # ------------------------------------------------------------------
    print(f"\n[4/5] Checking backend health...")
    health = run_az(
        f"network application-gateway show-backend-health "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw",
        check=False,
    )
    if health and isinstance(health, dict):
        backend_health = []
        for pool in health.get("backendAddressPools", []):
            for setting in pool.get("backendSettings", pool.get("backendHttpSettingsCollection", [])):
                for server in setting.get("servers", []):
                    entry = {
                        "address": server.get("address", ""),
                        "health": server.get("health", ""),
                        "healthProbeLog": server.get("healthProbeLog", ""),
                    }
                    backend_health.append(entry)
                    status_icon = "OK" if entry["health"] == "Healthy" else entry["health"]
                    print(f"  {entry['address']}: {status_icon}")
        resources["backendHealth"] = backend_health
    else:
        print("  Could not retrieve backend health")
        resources["backendHealth"] = None
    save_resources(resources)

    # ------------------------------------------------------------------
    # Step 5: Add IP to Aura BC allowlist
    # ------------------------------------------------------------------
    appgw_ip = resources.get("publicIp", {}).get("ipAddress", "")
    if not appgw_ip:
        print(f"\n[5/5] Skipping allowlist — no public IP found")
        resources["allowlist"] = None
    else:
        print(f"\n[5/5] Adding {appgw_ip} to Aura BC allowlist...")
        filter_id = add_to_allowlist(appgw_ip)
        resources["allowlist"] = {
            "ipAddress": appgw_ip,
            "filterId": filter_id,
            "instanceId": os.getenv("AURA_INSTANCE_ID", ""),
        }
    save_resources(resources)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)

    appgw_info = resources.get("applicationGateway", {})
    print(f"""
  Resource Group:     {RESOURCE_GROUP}
  App Gateway:        {appgw_info.get('name', 'N/A')}
  Provisioning State: {appgw_info.get('provisioningState', 'N/A')}
  Operational State:  {appgw_info.get('operationalState', 'N/A')}
  Public IP:          {appgw_ip or 'N/A'}
  Private Link:       {appgw_info.get('privateLinkConfigName', 'N/A')}
  Resource manifest:  {RESOURCES_FILE}
""")

    # Check readiness
    ready = True
    issues = []

    if appgw_info.get("provisioningState") != "Succeeded":
        ready = False
        issues.append(f"App Gateway provisioning: {appgw_info.get('provisioningState', 'unknown')}")

    if not appgw_ip:
        ready = False
        issues.append("Public IP not allocated")

    health_entries = resources.get("backendHealth") or []
    unhealthy = [e for e in health_entries if e.get("health") != "Healthy"]
    if unhealthy:
        ready = False
        for e in unhealthy:
            issues.append(f"Backend {e['address']}: {e['health']}")

    if not resources.get("allowlist", {}).get("filterId"):
        ready = False
        issues.append("IP not added to Aura BC allowlist")

    if ready:
        print("  Status: READY for Databricks NCC integration")
        print()
        print("  Next steps:")
        print("    uv run python deploy.py create-ncc --profile <databricks-cli-profile>")
        print("    uv run python deploy.py create-pe-rule --profile <databricks-cli-profile>")
        print("    uv run python deploy.py approve")
    else:
        print("  Status: NOT READY — issues found:")
        for issue in issues:
            print(f"    - {issue}")


if __name__ == "__main__":
    main()
