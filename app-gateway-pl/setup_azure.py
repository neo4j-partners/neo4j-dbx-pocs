"""
Phased deployment for the Application Gateway Private Link experiment.

Phase 1: Deploy a pure L7 App Gateway (no L4 properties), configure Private
         Link, create a Private Endpoint, and approve it. Tests whether PL
         validation passes without L4 listeners.

Phase 2: Update the same gateway to add L4 TCP listeners on port 7687.
         Tests whether the established PL tunnel continues to forward TCP
         traffic after L4 properties are added.

Commands:
    uv run python setup_azure.py phase1   — Deploy pure L7 gateway + PE
    uv run python setup_azure.py phase2   — Add L4 TCP listeners to existing gateway
    uv run python setup_azure.py status   — Show current state of all resources
    uv run python setup_azure.py cleanup  — Tear down all infrastructure
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "aurabc-appgw-poc-rg")
LOCATION = os.getenv("AZURE_LOCATION", "eastus")
DEPLOYMENT_NAME = "aurabc-appgw-poc"
PREFIX = os.getenv("AZURE_PREFIX", "aurabc-appgw")
RESOURCES_FILE = os.path.join(BASE_DIR, "azure-resources.json")

PHASE1_BICEP = os.path.join(BASE_DIR, "infra", "main-phase1.bicep")
PHASE2_BICEP = os.path.join(BASE_DIR, "infra", "main-phase2.bicep")

# PE resources — created in the same RG as the App Gateway
PE_VNET_NAME = f"{PREFIX}-pe-vnet"
PE_SUBNET_NAME = "pe-subnet"
PE_NAME = f"{PREFIX}-pe"
FRONTEND_IP_CONFIG_NAME = "appgw-frontend-ip"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def get_aura_fqdn():
    """Extract the Aura BC FQDN from NEO4J_URI."""
    aura_fqdn = os.getenv("NEO4J_URI", "")
    for scheme in ("neo4j+s://", "bolt+s://", "neo4j://", "bolt://"):
        if aura_fqdn.startswith(scheme):
            aura_fqdn = aura_fqdn[len(scheme):]
            break
    aura_fqdn = aura_fqdn.rstrip("/")
    if not aura_fqdn:
        print("ERROR: Set NEO4J_URI in .env")
        sys.exit(1)
    return aura_fqdn


def save_resources(resources):
    """Write the resource manifest to disk."""
    with open(RESOURCES_FILE, "w") as f:
        json.dump(resources, f, indent=2)


def load_resources():
    """Load the resource manifest."""
    if not os.path.exists(RESOURCES_FILE):
        return {}
    with open(RESOURCES_FILE) as f:
        return json.load(f)


def deploy_bicep(bicep_file, params):
    """Deploy a Bicep template and return the result."""
    resolved_path = os.path.join(BASE_DIR, "infra", ".params-resolved.json")
    resolved = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": params,
    }
    with open(resolved_path, "w") as f:
        json.dump(resolved, f, indent=2)

    result = run_az(
        f"deployment group create "
        f"--name {DEPLOYMENT_NAME} "
        f"--resource-group {RESOURCE_GROUP} "
        f"--template-file {bicep_file} "
        f"--parameters @{resolved_path}"
    )

    if os.path.exists(resolved_path):
        os.remove(resolved_path)

    return result


def query_appgw():
    """Query the App Gateway for full details."""
    return run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw",
        check=False,
    )


def query_pe_connections():
    """Query PE connections on the App Gateway."""
    return run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw "
        f"--query privateEndpointConnections",
        check=False,
    )


def query_pe_ip():
    """Query the private endpoint's NIC to get its private IP address."""
    pe = run_az(
        f"network private-endpoint show "
        f"--resource-group {RESOURCE_GROUP} --name {PE_NAME} "
        f"--query networkInterfaces[0].id --output tsv",
        parse_json=False, check=False,
    )
    if not pe or not pe.strip():
        return None

    nic_id = pe.strip()
    ip = run_az(
        f"network nic show --ids {nic_id} "
        f"--query ipConfigurations[0].privateIPAddress --output tsv",
        parse_json=False, check=False,
    )
    return ip.strip() if ip else None


def approve_pe_connections():
    """Approve any pending PE connections on the App Gateway."""
    pe_conns = query_pe_connections()
    if not pe_conns or not isinstance(pe_conns, list):
        print("  No PE connections found on App Gateway")
        return 0

    approved = 0
    for conn in pe_conns:
        props = conn.get("properties", conn)
        state = props.get("privateLinkServiceConnectionState", {})
        status = state.get("status", "")
        conn_id = conn.get("id", "")
        conn_name = conn_id.split("/")[-1] if conn_id else "unknown"

        if status == "Pending":
            print(f"  Approving: {conn_name}")
            body = json.dumps({
                "properties": {
                    "privateLinkServiceConnectionState": {
                        "status": "Approved",
                        "description": "Approved for Phase 1 experiment"
                    }
                }
            })
            run_az(
                f'rest --method PUT '
                f'--url "https://management.azure.com{conn_id}?api-version=2025-01-01" '
                f"--body '{body}'",
            )
            print(f"  Approved: {conn_name}")
            approved += 1
        else:
            print(f"  {conn_name}: {status}")

    return approved


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

    for line in result.stdout.splitlines():
        if line.startswith("Filter ID:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


# ---------------------------------------------------------------------------
# Phase 1: Pure L7 App Gateway + Private Link + PE
# ---------------------------------------------------------------------------

def cmd_phase1():
    """Deploy pure L7 gateway, create PE, approve it."""
    print("=" * 60)
    print("PHASE 1: Pure L7 App Gateway + Private Link")
    print("=" * 60)

    account = check_az_cli()
    aura_fqdn = get_aura_fqdn()

    resources = {
        "metadata": {
            "deployedAt": datetime.now(timezone.utc).isoformat(),
            "phase": "phase1",
            "deploymentName": DEPLOYMENT_NAME,
            "subscriptionId": account["id"],
            "location": LOCATION,
            "prefix": PREFIX,
            "auraFqdn": aura_fqdn,
        },
    }

    print(f"\n  Aura BC FQDN: {aura_fqdn}")
    print(f"  Resource Group: {RESOURCE_GROUP}")
    print(f"  Location: {LOCATION}")

    # Step 1: Create resource group
    print(f"\n[1/6] Creating resource group...")
    rg = run_az(f"group create --name {RESOURCE_GROUP} --location {LOCATION}")
    if rg is None:
        sys.exit(1)

    resources["resourceGroup"] = {
        "name": RESOURCE_GROUP,
        "location": LOCATION,
        "resourceId": rg.get("id", ""),
    }
    save_resources(resources)

    # Step 2: Deploy Phase 1 Bicep (pure L7, no L4 properties)
    print(f"\n[2/6] Deploying Phase 1 Bicep — pure L7 gateway (5-10 minutes)...")
    params = {
        "location": {"value": LOCATION},
        "prefix": {"value": PREFIX},
        "auraFqdn": {"value": aura_fqdn},
    }
    result = deploy_bicep(PHASE1_BICEP, params)
    if result is None:
        print("\nPhase 1 deployment FAILED.")
        save_resources(resources)
        sys.exit(1)

    # Extract outputs
    bicep_outputs = {}
    raw_outputs = result.get("properties", {}).get("outputs", {})
    for key, val in raw_outputs.items():
        bicep_outputs[key] = val.get("value", "")
    resources["metadata"]["bicepOutputs"] = bicep_outputs

    # Query App Gateway
    print(f"\n[3/6] Querying deployed resources...")
    appgw = query_appgw()
    if appgw and isinstance(appgw, dict):
        resources["applicationGateway"] = {
            "name": appgw.get("name", ""),
            "resourceId": appgw.get("id", ""),
            "provisioningState": appgw.get("provisioningState", ""),
            "operationalState": appgw.get("operationalState", ""),
            "sku": appgw.get("sku", {}).get("name", ""),
            "privateLinkConfigName": "pl-config",
            "frontendIpConfigName": FRONTEND_IP_CONFIG_NAME,
            "hasL4Listeners": len(appgw.get("listeners", [])) > 0,
            "hasL7Listeners": len(appgw.get("httpListeners", [])) > 0,
        }

    pip = run_az(
        f"network public-ip show --resource-group {RESOURCE_GROUP} --name {PREFIX}-pip",
        check=False,
    )
    if pip and isinstance(pip, dict):
        resources["publicIp"] = {
            "name": pip.get("name", ""),
            "ipAddress": pip.get("ipAddress", ""),
        }

    save_resources(resources)

    appgw_ip = resources.get("publicIp", {}).get("ipAddress", "")
    print(f"  App Gateway: {resources.get('applicationGateway', {}).get('provisioningState', 'unknown')}")
    print(f"  Public IP: {appgw_ip}")
    print(f"  L7 listeners: {resources.get('applicationGateway', {}).get('hasL7Listeners', False)}")
    print(f"  L4 listeners: {resources.get('applicationGateway', {}).get('hasL4Listeners', False)}")

    # Step 4: Add IP to Aura BC allowlist
    if appgw_ip:
        print(f"\n[4/6] Adding {appgw_ip} to Aura BC allowlist...")
        filter_id = add_to_allowlist(appgw_ip)
        resources["allowlist"] = {
            "ipAddress": appgw_ip,
            "filterId": filter_id,
        }
    else:
        print(f"\n[4/6] Skipping allowlist — no public IP found")
        resources["allowlist"] = None
    save_resources(resources)

    # Step 5: Create PE VNet and Private Endpoint
    print(f"\n[5/6] Creating Private Endpoint to App Gateway...")
    appgw_id = resources.get("applicationGateway", {}).get("resourceId", "")
    if not appgw_id:
        print("ERROR: No App Gateway resource ID found.")
        sys.exit(1)

    # Create PE VNet
    print("  Creating PE VNet...")
    run_az(
        f"network vnet create "
        f"--resource-group {RESOURCE_GROUP} --name {PE_VNET_NAME} "
        f"--location {LOCATION} --address-prefix 10.1.0.0/16 "
        f"--subnet-name {PE_SUBNET_NAME} --subnet-prefix 10.1.1.0/24"
    )

    # Create Private Endpoint
    # group-id is the frontend IP configuration name
    print("  Creating Private Endpoint...")
    pe_result = run_az(
        f"network private-endpoint create "
        f"--resource-group {RESOURCE_GROUP} --name {PE_NAME} "
        f"--vnet-name {PE_VNET_NAME} --subnet {PE_SUBNET_NAME} "
        f"--private-connection-resource-id {appgw_id} "
        f"--group-id {FRONTEND_IP_CONFIG_NAME} "
        f"--connection-name {PE_NAME}-connection "
        f"--location {LOCATION}",
        check=False,
    )

    if pe_result is None:
        print("\n" + "=" * 60)
        print("PHASE 1 RESULT: FAIL")
        print("=" * 60)
        print("\n  PE creation failed. The pure L7 gateway did not satisfy")
        print("  Private Link validation either.")
        print("\n  Check the error above. If it is the same")
        print("  ApplicationGatewayPrivateLinkOperationError, then App Gateway")
        print("  Private Link may have additional constraints beyond httpListeners.")
        print("\n  Next step: file a Microsoft support ticket.")
        save_resources(resources)
        sys.exit(1)

    print("  PE created successfully!")

    # Step 6: Approve PE and query IP
    print(f"\n[6/6] Approving PE connection...")
    time.sleep(5)  # brief wait for PE to register on the gateway
    approved = approve_pe_connections()

    pe_ip = query_pe_ip()
    resources["privateEndpoint"] = {
        "name": PE_NAME,
        "privateIp": pe_ip,
        "vnetName": PE_VNET_NAME,
        "subnetName": PE_SUBNET_NAME,
    }
    save_resources(resources)

    # Summary
    print()
    print("=" * 60)
    print("PHASE 1 RESULT: PASS")
    print("=" * 60)
    print(f"""
  App Gateway:     {PREFIX}-gw (pure L7, no L4 listeners)
  Public IP:       {appgw_ip}
  Private Link:    pl-config
  PE Name:         {PE_NAME}
  PE Private IP:   {pe_ip or 'pending'}
  PE Approved:     {approved > 0}

  The pure L7 gateway accepted a Private Endpoint connection.
  Private Link tunnel is established.

  Next step:
    uv run python setup_azure.py phase2
""")


# ---------------------------------------------------------------------------
# Phase 2: Add L4 TCP Listeners
# ---------------------------------------------------------------------------

def cmd_phase2():
    """Update the gateway to add L4 TCP listeners."""
    print("=" * 60)
    print("PHASE 2: Add L4 TCP Listeners to Existing Gateway")
    print("=" * 60)

    check_az_cli()
    aura_fqdn = get_aura_fqdn()
    resources = load_resources()
    resources["metadata"]["auraFqdn"] = aura_fqdn

    if not resources.get("applicationGateway"):
        print("ERROR: No Phase 1 deployment found. Run phase1 first.")
        sys.exit(1)

    # Step 1: Verify Phase 1 PE is Approved
    print(f"\n[1/3] Verifying Phase 1 PE connection...")
    pe_conns = query_pe_connections()
    pe_ok = False
    if pe_conns and isinstance(pe_conns, list):
        for conn in pe_conns:
            props = conn.get("properties", conn)
            state = props.get("privateLinkServiceConnectionState", {})
            status = state.get("status", "")
            conn_name = conn.get("id", "").split("/")[-1]
            print(f"  {conn_name}: {status}")
            if status == "Approved":
                pe_ok = True

    if not pe_ok:
        print("\n  WARNING: No Approved PE connections found.")
        print("  Phase 2 will proceed, but PE may need re-approval after update.")

    # Step 2: Deploy Phase 2 Bicep (adds L4 listeners)
    print(f"\n[2/3] Deploying Phase 2 Bicep — adding L4 TCP listeners (5-15 minutes)...")
    print("  This updates the existing gateway. The PL tunnel may briefly disconnect.")
    params = {
        "location": {"value": LOCATION},
        "prefix": {"value": PREFIX},
        "auraFqdn": {"value": aura_fqdn},
        "auraPort": {"value": 7687},
    }
    result = deploy_bicep(PHASE2_BICEP, params)

    if result is None:
        print()
        print("=" * 60)
        print("PHASE 2 RESULT: DEPLOYMENT FAILED")
        print("=" * 60)
        print("\n  Azure rejected the update. L4 properties cannot be added to")
        print("  a Private Link-enabled gateway.")
        print("\n  This confirms that the App Gateway L4 + Private Link")
        print("  incompatibility is enforced at deployment time, not just")
        print("  at PE creation time.")
        resources["metadata"]["phase"] = "phase2-failed"
        save_resources(resources)
        sys.exit(1)

    # Step 3: Verify post-update state
    print(f"\n[3/3] Verifying post-update state...")

    # Brief pause for gateway to stabilize
    print("  Waiting 10s for gateway to stabilize...")
    time.sleep(10)

    # Check App Gateway
    appgw = query_appgw()
    appgw_ok = False
    has_l4 = False
    if appgw and isinstance(appgw, dict):
        prov_state = appgw.get("provisioningState", "")
        op_state = appgw.get("operationalState", "")
        l4_listeners = appgw.get("listeners", [])
        l7_listeners = appgw.get("httpListeners", [])
        has_l4 = len(l4_listeners) > 0
        appgw_ok = prov_state == "Succeeded"

        print(f"  Provisioning State: {prov_state}")
        print(f"  Operational State:  {op_state}")
        print(f"  L7 Listeners:       {len(l7_listeners)}")
        print(f"  L4 Listeners:       {len(l4_listeners)}")

        for l in l4_listeners:
            proto = l.get("properties", l).get("protocol", l.get("protocol", ""))
            print(f"    - {l.get('name', 'unnamed')} ({proto})")

        resources["applicationGateway"]["provisioningState"] = prov_state
        resources["applicationGateway"]["operationalState"] = op_state
        resources["applicationGateway"]["hasL4Listeners"] = has_l4
    else:
        print("  ERROR: Could not query App Gateway after update")

    # Check PE connection status
    print()
    pe_conns = query_pe_connections()
    pe_still_ok = False
    if pe_conns and isinstance(pe_conns, list):
        for conn in pe_conns:
            props = conn.get("properties", conn)
            state = props.get("privateLinkServiceConnectionState", {})
            status = state.get("status", "")
            conn_name = conn.get("id", "").split("/")[-1]
            print(f"  PE {conn_name}: {status}")
            if status == "Approved":
                pe_still_ok = True
    else:
        print("  No PE connections found")

    # Update resources
    resources["metadata"]["phase"] = "phase2"
    save_resources(resources)

    # Summary
    print()
    print("=" * 60)

    if appgw_ok and has_l4 and pe_still_ok:
        print("PHASE 2 RESULT: PASS — L4 added, PL tunnel intact")
        print("=" * 60)
        print(f"""
  The gateway now has both L7 and L4 listeners, and the PE
  connection remains Approved.

  The Private Link tunnel should forward TCP traffic on port 7687
  to the L4 listener.

  Next steps:
    1. Deploy the test VM to validate end-to-end Bolt connectivity:
       cd py-test && uv run python deploy_test_vm.py deploy

    2. Or test directly with validate_bolt.py against the PE IP:
       uv run python setup_azure.py status   (to see the PE IP)
""")

    elif appgw_ok and has_l4 and not pe_still_ok:
        print("PHASE 2 RESULT: PARTIAL — L4 added, PL tunnel broke")
        print("=" * 60)
        print(f"""
  The gateway update succeeded and L4 listeners are present, but
  the PE connection is no longer Approved.

  Try re-creating the PE:
    az network private-endpoint delete --resource-group {RESOURCE_GROUP} --name {PE_NAME}
    az network private-endpoint create \\
      --resource-group {RESOURCE_GROUP} --name {PE_NAME} \\
      --vnet-name {PE_VNET_NAME} --subnet {PE_SUBNET_NAME} \\
      --private-connection-resource-id <appgw-id> \\
      --group-id {FRONTEND_IP_CONFIG_NAME} \\
      --connection-name {PE_NAME}-connection \\
      --location {LOCATION}
""")

    elif not appgw_ok:
        print("PHASE 2 RESULT: FAIL — Gateway update failed")
        print("=" * 60)
        print("\n  The gateway is in a bad state after the update.")
        print("  Check provisioning errors in the Azure portal.")

    else:
        print("PHASE 2 RESULT: UNKNOWN")
        print("=" * 60)
        print("  Review the output above to determine the state.")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cmd_status():
    """Show current state of all deployed resources."""
    print("=" * 60)
    print("DEPLOYMENT STATUS")
    print("=" * 60)

    check_az_cli()

    exists = run_az(f"group exists --name {RESOURCE_GROUP}", parse_json=True)
    if not exists:
        print(f"\nResource group '{RESOURCE_GROUP}' does not exist. Nothing deployed.")
        return

    resources = load_resources()
    phase = resources.get("metadata", {}).get("phase", "unknown")
    print(f"\n  Resource Group: {RESOURCE_GROUP}")
    print(f"  Phase:          {phase}")

    # App Gateway
    print("\n--- Application Gateway ---")
    appgw = query_appgw()
    if appgw and isinstance(appgw, dict):
        print(f"  Provisioning State: {appgw.get('provisioningState', 'unknown')}")
        print(f"  Operational State:  {appgw.get('operationalState', 'unknown')}")

        l7 = appgw.get("httpListeners", [])
        l4 = appgw.get("listeners", [])
        print(f"  L7 Listeners:       {len(l7)}")
        for lis in l7:
            proto = lis.get("properties", lis).get("protocol", lis.get("protocol", ""))
            print(f"    - {lis.get('name', 'unnamed')} ({proto})")
        print(f"  L4 Listeners:       {len(l4)}")
        for lis in l4:
            proto = lis.get("properties", lis).get("protocol", lis.get("protocol", ""))
            print(f"    - {lis.get('name', 'unnamed')} ({proto})")

        pl_configs = appgw.get("privateLinkConfigurations", [])
        print(f"  Private Link Configs: {len(pl_configs)}")
        for plc in pl_configs:
            print(f"    - {plc.get('name', 'unnamed')}")
    else:
        print("  Application Gateway not found")

    # Public IP
    print("\n--- Public IP ---")
    ip = run_az(
        f"network public-ip show --resource-group {RESOURCE_GROUP} --name {PREFIX}-pip "
        f"--query ipAddress --output tsv",
        check=False, parse_json=False,
    )
    print(f"  IP Address: {ip or 'NOT ALLOCATED'}")

    # PE connections on App Gateway
    print("\n--- Private Endpoint Connections (on App Gateway) ---")
    pe_conns = query_pe_connections()
    if pe_conns and isinstance(pe_conns, list) and len(pe_conns) > 0:
        for conn in pe_conns:
            props = conn.get("properties", conn)
            state = props.get("privateLinkServiceConnectionState", {})
            status = state.get("status", "")
            conn_name = conn.get("id", "").split("/")[-1]
            print(f"  {conn_name}: {status}")
    else:
        print("  No PE connections")

    # PE resource
    print("\n--- Private Endpoint ---")
    pe_ip = query_pe_ip()
    if pe_ip:
        print(f"  Name:       {PE_NAME}")
        print(f"  Private IP: {pe_ip}")
    else:
        print("  Private Endpoint not found")

    # PE VNet
    print("\n--- PE VNet ---")
    pe_vnet = run_az(
        f"network vnet show --resource-group {RESOURCE_GROUP} --name {PE_VNET_NAME}",
        check=False,
    )
    if pe_vnet and isinstance(pe_vnet, dict):
        print(f"  Name: {pe_vnet.get('name', '')}")
        for subnet in pe_vnet.get("subnets", []):
            print(f"  Subnet: {subnet.get('name', '')} ({subnet.get('addressPrefix', '')})")
    else:
        print("  PE VNet not found")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cmd_cleanup():
    """Delete all deployed infrastructure."""
    print("=" * 60)
    print("CLEANUP — Deleting all POC infrastructure")
    print("=" * 60)

    check_az_cli()

    exists = run_az(f"group exists --name {RESOURCE_GROUP}", parse_json=True)
    if not exists:
        print(f"\nResource group '{RESOURCE_GROUP}' does not exist. Nothing to clean up.")
        return

    print(f"\nThis will DELETE the entire resource group '{RESOURCE_GROUP}' and all resources in it.")
    confirm = input("Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Cleanup cancelled.")
        return

    print("\nDeleting resource group (this takes 1-2 minutes)...")
    run_az(f"group delete --name {RESOURCE_GROUP} --yes", parse_json=False)

    if os.path.exists(RESOURCES_FILE):
        os.remove(RESOURCES_FILE)

    print("Cleanup complete. All Azure resources deleted.")
    print("\nNOTE: If you added the App Gateway IP to the Aura BC allowlist, remove it:")
    print("  uv run python manage_ip_allowlist.py list")
    print("  uv run python manage_ip_allowlist.py remove --filter-id <ID>")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phased App Gateway Private Link experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  phase1    Deploy pure L7 gateway + Private Link + PE
  phase2    Add L4 TCP listeners to existing gateway
  status    Show status of all deployed resources
  cleanup   Tear down all infrastructure
        """,
    )
    parser.add_argument("command", choices=["phase1", "phase2", "status", "cleanup"])
    args = parser.parse_args()

    commands = {
        "phase1": cmd_phase1,
        "phase2": cmd_phase2,
        "status": cmd_status,
        "cleanup": cmd_cleanup,
    }
    commands[args.command]()


if __name__ == "__main__":
    main()
