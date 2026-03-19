"""
Deploy and run the Application Gateway private link validation test VM.

Self-contained — reads azure-resources.json and parent .env for all config.
No imports from the parent project.

Workflow:
    uv run python deploy_test_vm.py deploy    — Deploy test VM, approve PE, SCP test files
    uv run python deploy_test_vm.py test      — Run pytest on the VM via SSH
    uv run python deploy_test_vm.py ssh       — SSH into the VM for debugging
    uv run python deploy_test_vm.py cleanup   — Delete all test VM resources
"""

import argparse
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)

load_dotenv(os.path.join(PARENT_DIR, ".env"))

RESOURCE_GROUP = os.getenv("AZURE_TEST_RESOURCE_GROUP", "appgw-test-rg")
LOCATION = os.getenv("AZURE_TEST_LOCATION", "eastus")
PREFIX = os.getenv("AZURE_TEST_PREFIX", "appgw-test")
DEPLOYMENT_NAME = "appgw-test-vm"
BICEP_FILE = os.path.join(BASE_DIR, "infra", "main.bicep")
OUTPUTS_FILE = os.path.join(BASE_DIR, "test-vm-outputs.json")
RESOURCES_FILE = os.path.join(PARENT_DIR, "azure-resources.json")
ADMIN_USER = "azureuser"
REMOTE_DIR = "/opt/appgw-test"

# Files to SCP to the VM
TEST_FILES = ["pyproject.toml", "conftest.py", "test_appgw_connectivity.py"]


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


def load_parent_resources():
    """Load azure-resources.json from the parent App Gateway infrastructure."""
    if not os.path.exists(RESOURCES_FILE):
        print(f"ERROR: {RESOURCES_FILE} not found.")
        print("Run 'uv run python setup_azure.py' in the parent directory first.")
        sys.exit(1)
    with open(RESOURCES_FILE) as f:
        return json.load(f)


def find_ssh_key():
    """Find an SSH public key."""
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = os.path.expanduser(f"~/.ssh/{name}")
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    print("ERROR: No SSH public key found at ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub")
    sys.exit(1)


def resolve_params():
    """Build Bicep parameters from parent azure-resources.json and .env."""
    resources = load_parent_resources()

    appgw = resources.get("applicationGateway", {})
    appgw_resource_id = appgw.get("resourceId", "")
    pl_config_name = appgw.get("privateLinkConfigName", "")

    if not appgw_resource_id:
        print("ERROR: applicationGateway.resourceId not found in azure-resources.json")
        sys.exit(1)
    if not pl_config_name:
        print("ERROR: applicationGateway.privateLinkConfigName not found in azure-resources.json")
        sys.exit(1)

    aura_fqdn = os.getenv("NEO4J_URI", "")
    for scheme in ("neo4j+s://", "bolt+s://", "neo4j://", "bolt://"):
        if aura_fqdn.startswith(scheme):
            aura_fqdn = aura_fqdn[len(scheme):]
            break
    aura_fqdn = aura_fqdn.rstrip("/")
    if not aura_fqdn:
        print("ERROR: NEO4J_URI not set in parent .env")
        sys.exit(1)

    return {
        "location": {"value": LOCATION},
        "prefix": {"value": PREFIX},
        "appGwResourceId": {"value": appgw_resource_id},
        "plConfigName": {"value": pl_config_name},
        "auraFqdn": {"value": aura_fqdn},
        "adminUsername": {"value": ADMIN_USER},
        "sshPublicKey": {"value": find_ssh_key()},
    }


def save_outputs(outputs):
    """Save deployment outputs to JSON."""
    flat = {k: v.get("value", "") for k, v in outputs.items()}
    with open(OUTPUTS_FILE, "w") as f:
        json.dump(flat, f, indent=2)
    print(f"\n  Outputs saved to {OUTPUTS_FILE}")
    return flat


def load_outputs():
    """Load test VM deployment outputs."""
    if not os.path.exists(OUTPUTS_FILE):
        print(f"ERROR: {OUTPUTS_FILE} not found. Run 'deploy' first.")
        sys.exit(1)
    with open(OUTPUTS_FILE) as f:
        return json.load(f)


def generate_vm_env(vm_outputs):
    """Generate a .env file for the test VM with everything the tests need."""
    resources = load_parent_resources()
    appgw_ip = resources.get("publicIp", {}).get("ipAddress", "")

    env_lines = [
        f"NEO4J_URI={os.getenv('NEO4J_URI', '')}",
        f"NEO4J_USERNAME={os.getenv('NEO4J_USERNAME', '')}",
        f"NEO4J_PASSWORD={os.getenv('NEO4J_PASSWORD', '')}",
        f"AURA_API_CLIENT_ID={os.getenv('AURA_API_CLIENT_ID', '')}",
        f"AURA_API_CLIENT_SECRET={os.getenv('AURA_API_CLIENT_SECRET', '')}",
        f"AURA_ORG_ID={os.getenv('AURA_ORG_ID', '')}",
        f"AURA_INSTANCE_ID={os.getenv('AURA_INSTANCE_ID', '')}",
        f"APPGW_IP={appgw_ip}",
        f"PE_IP={vm_outputs.get('privateEndpointIp', '')}",
    ]
    env_path = os.path.join(BASE_DIR, ".env.vm")
    with open(env_path, "w") as f:
        f.write("\n".join(env_lines) + "\n")
    print(f"  Generated {env_path}")
    return env_path


def query_pe_ip(pe_name):
    """Query the private endpoint's NIC to get its private IP address."""
    pe = run_az(
        f"network private-endpoint show "
        f"--resource-group {RESOURCE_GROUP} --name {pe_name} "
        f"--query networkInterfaces[0].id --output tsv",
        parse_json=False,
    )
    if not pe:
        print("ERROR: Could not find private endpoint NIC.")
        sys.exit(1)

    nic_id = pe.strip()
    ip = run_az(
        f"network nic show --ids {nic_id} "
        f"--query ipConfigurations[0].privateIPAddress --output tsv",
        parse_json=False,
    )
    if not ip:
        print("ERROR: Could not determine PE private IP.")
        sys.exit(1)

    return ip.strip()


def approve_pe_on_appgw():
    """Approve the pending PE connection on the Application Gateway."""
    resources = load_parent_resources()
    appgw_name = resources.get("applicationGateway", {}).get("name", "")
    appgw_rg = resources.get("resourceGroup", {}).get("name", "")

    if not appgw_name or not appgw_rg:
        print("  WARNING: Could not determine App Gateway name/RG for PE approval")
        return

    print(f"  Checking {appgw_name} for pending PE connections...")
    pe_conns = run_az(
        f"network application-gateway show "
        f"--resource-group {appgw_rg} --name {appgw_name} "
        f"--query privateEndpointConnections",
        check=False,
    )

    if not pe_conns or not isinstance(pe_conns, list):
        print("  No PE connections found on App Gateway")
        return

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
                        "description": "Approved for test VM"
                    }
                }
            })
            run_az(
                f'rest --method PUT '
                f'--url "https://management.azure.com{conn_id}?api-version=2025-01-01" '
                f"--body '{body}'",
            )
            print(f"  Approved: {conn_name}")
        else:
            print(f"  {conn_name}: {status}")


def configure_hosts_entry(vm_ip, pe_ip, aura_fqdn):
    """Add the Aura FQDN → PE IP mapping to /etc/hosts on the VM via SSH."""
    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
    print(f"\n  Configuring /etc/hosts: {aura_fqdn} → {pe_ip}")
    result = subprocess.run(
        f'ssh {ssh_opts} {ADMIN_USER}@{vm_ip} '
        f'"echo \'{pe_ip} {aura_fqdn}\' | sudo tee -a /etc/hosts"',
        shell=True, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR: Failed to update /etc/hosts: {result.stderr.strip()}")
        sys.exit(1)
    print(f"  /etc/hosts updated")


def wait_for_ssh(ip, timeout=180):
    """Wait until SSH is reachable."""
    import socket
    print(f"\n  Waiting for SSH on {ip}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            sock = socket.create_connection((ip, 22), timeout=5)
            sock.close()
            print(f"  SSH reachable after {int(time.time() - start)}s")
            return True
        except (OSError, socket.timeout):
            time.sleep(5)
    print(f"  WARNING: SSH not reachable after {timeout}s")
    return False


def scp_files(vm_ip):
    """SCP test files and .env to the VM."""
    print(f"\n  Copying test files to {vm_ip}:{REMOTE_DIR}/...")

    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

    subprocess.run(
        f'ssh {ssh_opts} {ADMIN_USER}@{vm_ip} "mkdir -p {REMOTE_DIR}"',
        shell=True, check=True,
    )

    for filename in TEST_FILES:
        local = os.path.join(BASE_DIR, filename)
        subprocess.run(
            f"scp {ssh_opts} {local} {ADMIN_USER}@{vm_ip}:{REMOTE_DIR}/{filename}",
            shell=True, check=True,
        )
        print(f"    {filename}")

    env_path = os.path.join(BASE_DIR, ".env.vm")
    subprocess.run(
        f"scp {ssh_opts} {env_path} {ADMIN_USER}@{vm_ip}:{REMOTE_DIR}/.env",
        shell=True, check=True,
    )
    print("    .env")


def wait_for_cloud_init(vm_ip):
    """Wait for cloud-init to finish on the VM."""
    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
    print("\n  Waiting for cloud-init to complete...")

    for attempt in range(30):
        result = subprocess.run(
            f'ssh {ssh_opts} {ADMIN_USER}@{vm_ip} "cloud-init status --wait 2>/dev/null || cloud-init status"',
            shell=True, capture_output=True, text=True,
        )
        output = result.stdout.strip()
        if "done" in output:
            print(f"  cloud-init finished")
            return True
        if "error" in output:
            print(f"  WARNING: cloud-init reported errors: {output}")
            return True
        time.sleep(10)

    print("  WARNING: cloud-init did not finish in time")
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_deploy():
    """Deploy the test VM, approve PE, SCP files, and write outputs."""
    print("=" * 60)
    print("DEPLOYING TEST VM")
    print(f"  Region: {LOCATION}")
    print(f"  Resource Group: {RESOURCE_GROUP}")
    print("=" * 60)

    check_az_cli()
    params = resolve_params()

    print(f"\n  App GW: {params['appGwResourceId']['value'].split('/')[-1]}")
    print(f"  PL Config: {params['plConfigName']['value']}")
    print(f"  Aura FQDN: {params['auraFqdn']['value']}")

    # Create resource group
    print("\n[1/7] Creating resource group...")
    run_az(f"group create --name {RESOURCE_GROUP} --location {LOCATION}")

    # Write resolved params
    resolved_path = os.path.join(BASE_DIR, "infra", ".params-resolved.json")
    resolved = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": params,
    }
    with open(resolved_path, "w") as f:
        json.dump(resolved, f, indent=2)

    # Deploy Bicep
    print("\n[2/7] Deploying Bicep template (3-5 minutes)...")
    result = run_az(
        f"deployment group create "
        f"--name {DEPLOYMENT_NAME} "
        f"--resource-group {RESOURCE_GROUP} "
        f"--template-file {BICEP_FILE} "
        f"--parameters @{resolved_path}"
    )

    if os.path.exists(resolved_path):
        os.remove(resolved_path)

    if result is None:
        print("\nDeployment FAILED.")
        sys.exit(1)

    # Fetch and save outputs
    print("\n[3/7] Fetching deployment outputs...")
    outputs = run_az(
        f"deployment group show --resource-group {RESOURCE_GROUP} --name {DEPLOYMENT_NAME} "
        f"--query properties.outputs",
    )
    if not outputs or not isinstance(outputs, dict):
        print("ERROR: Could not fetch outputs.")
        sys.exit(1)

    flat = save_outputs(outputs)
    vm_ip = flat.get("vmPublicIp", "")
    pe_name = flat.get("privateEndpointName", "")
    aura_fqdn = flat.get("auraFqdn", "")
    print(f"\n  VM Public IP: {vm_ip}")

    # Approve the PE connection on the App Gateway
    print("\n[4/7] Approving PE connection on Application Gateway...")
    approve_pe_on_appgw()

    # Query the PE NIC to get the private IP
    print("\n[5/7] Querying private endpoint IP...")
    pe_ip = query_pe_ip(pe_name)
    print(f"  PE Private IP: {pe_ip}")

    flat["privateEndpointIp"] = pe_ip
    with open(OUTPUTS_FILE, "w") as f:
        json.dump(flat, f, indent=2)

    # Generate .env for the VM
    print("\n[6/7] Generating VM environment...")
    generate_vm_env(flat)

    # Wait for VM, configure /etc/hosts, and SCP files
    print("\n[7/7] Setting up test environment on VM...")
    wait_for_ssh(vm_ip)
    wait_for_cloud_init(vm_ip)
    configure_hosts_entry(vm_ip, pe_ip, aura_fqdn)
    scp_files(vm_ip)

    print("\n" + "=" * 60)
    print("DEPLOY COMPLETE")
    print("=" * 60)
    print(f"\n  VM IP: {vm_ip}")
    print(f"  PE IP: {pe_ip}")
    print(f"  /etc/hosts: {aura_fqdn} → {pe_ip}")
    print(f"\nNext step:")
    print(f"  uv run python deploy_test_vm.py test")


def cmd_test():
    """Run pytest on the VM via SSH."""
    outputs = load_outputs()
    vm_ip = outputs.get("vmPublicIp", "")
    if not vm_ip:
        print("ERROR: vmPublicIp not in outputs.")
        sys.exit(1)

    print("=" * 60)
    print(f"RUNNING TESTS ON {vm_ip}")
    print("=" * 60)

    ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
    test_cmd = f"cd {REMOTE_DIR} && /usr/local/bin/uv run pytest -v -s 2>&1"

    result = subprocess.run(
        f'ssh {ssh_opts} {ADMIN_USER}@{vm_ip} "{test_cmd}"',
        shell=True,
    )

    print("\n" + "=" * 60)
    if result.returncode == 0:
        print("ALL TESTS PASSED")
    else:
        print(f"TESTS FAILED (exit code {result.returncode})")
    print("=" * 60)

    sys.exit(result.returncode)


def cmd_ssh():
    """SSH into the test VM for debugging."""
    outputs = load_outputs()
    vm_ip = outputs.get("vmPublicIp", "")
    if not vm_ip:
        print("ERROR: vmPublicIp not in outputs.")
        sys.exit(1)

    print(f"SSH into {vm_ip}...")
    os.execvp("ssh", [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{ADMIN_USER}@{vm_ip}",
    ])


def cmd_cleanup():
    """Delete all test VM resources."""
    print("=" * 60)
    print("CLEANUP — Deleting test VM resources")
    print("=" * 60)

    check_az_cli()

    exists = run_az(f"group exists --name {RESOURCE_GROUP}", parse_json=True)
    if not exists:
        print(f"\nResource group '{RESOURCE_GROUP}' does not exist.")
        return

    print(f"\nThis will DELETE '{RESOURCE_GROUP}' and all resources in it.")
    confirm = input("Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Cancelled.")
        return

    print("\nDeleting resource group...")
    run_az(f"group delete --name {RESOURCE_GROUP} --yes", parse_json=False)

    for path in (OUTPUTS_FILE, os.path.join(BASE_DIR, ".env.vm")):
        if os.path.exists(path):
            os.remove(path)

    print("Cleanup complete.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Deploy and test the Application Gateway private link validation VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  deploy    Deploy test VM in eastus, approve PE, SCP test files
  test      Run pytest on the VM via SSH
  ssh       SSH into the VM for debugging
  cleanup   Delete all test VM resources
        """,
    )
    parser.add_argument("command", choices=["deploy", "test", "ssh", "cleanup"])
    args = parser.parse_args()

    {"deploy": cmd_deploy, "test": cmd_test, "ssh": cmd_ssh, "cleanup": cmd_cleanup}[args.command]()


if __name__ == "__main__":
    main()
