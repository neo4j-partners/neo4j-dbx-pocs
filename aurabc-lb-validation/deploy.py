"""
Deployment management for the Aura BC Load Balancer POC.

Commands:
    uv run python deploy.py deploy        — Deploy all Azure infrastructure
    uv run python deploy.py allowlist     — Add NAT Gateway IP to Aura BC allowlist
    uv run python deploy.py status        — Show status of deployed resources
    uv run python deploy.py outputs       — Show deployment outputs (NAT IP, PLS ID, etc.)
    uv run python deploy.py ssh           — SSH into the proxy VM
    uv run python deploy.py create-ncc    — Create a Databricks NCC
    uv run python deploy.py create-pe-rule — Create private endpoint rule in NCC
    uv run python deploy.py approve       — Approve pending PLS connections
    uv run python deploy.py attach-ncc    — Attach NCC to a Databricks workspace
    uv run python deploy.py setup-secrets — Store Neo4j credentials in Databricks secrets
    uv run python deploy.py detach-ncc    — Detach and delete NCC from Databricks
    uv run python deploy.py cleanup       — Tear down all infrastructure
"""

import argparse
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "aurabc-lb-poc-rg")
LOCATION = os.getenv("AZURE_LOCATION", "eastus")
DEPLOYMENT_NAME = "aurabc-lb-poc"
BICEP_FILE = os.path.join(BASE_DIR, "infra", "main.bicep")
PARAMS_FILE = os.path.join(BASE_DIR, "infra", "parameters.json")
OUTPUTS_FILE = os.path.join(BASE_DIR, "deployment-outputs.json")
PREFIX = os.getenv("AZURE_PREFIX", "aurabc-lb")

DATABRICKS_BASE_URL = "https://accounts.azuredatabricks.net/api/2.0/accounts"
NCC_PLACEHOLDER_NAME = "neo4j-ncc-placeholder"


def run_az(cmd, check=True, parse_json=True):
    """Run an az CLI command and return the result."""
    full_cmd = f"az {cmd}"
    if parse_json and "--output" not in cmd and "-o " not in cmd:
        full_cmd += " --output json"

    print(f"  > {full_cmd}")
    result = subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
    )

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

    # Override from .env if available
    aura_fqdn = os.getenv("NEO4J_URI", "")
    if aura_fqdn.startswith("neo4j+s://"):
        aura_fqdn = aura_fqdn.replace("neo4j+s://", "")
    elif aura_fqdn.startswith("bolt+s://"):
        aura_fqdn = aura_fqdn.replace("bolt+s://", "")

    if aura_fqdn and aura_fqdn != "REPLACE_WITH_AURA_FQDN":
        params["auraFqdn"]["value"] = aura_fqdn

    ssh_key_path = os.path.expanduser("~/.ssh/id_rsa.pub")
    if os.path.exists(ssh_key_path):
        with open(ssh_key_path) as f:
            params["sshPublicKey"]["value"] = f.read().strip()

    # Also check id_ed25519.pub
    ed25519_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
    if params["sshPublicKey"]["value"] == "REPLACE_WITH_SSH_PUBLIC_KEY" and os.path.exists(ed25519_path):
        with open(ed25519_path) as f:
            params["sshPublicKey"]["value"] = f.read().strip()

    # Validate
    if params["auraFqdn"]["value"] == "REPLACE_WITH_AURA_FQDN":
        print("ERROR: Set NEO4J_URI in .env or auraFqdn in infra/parameters.json")
        sys.exit(1)
    if params["sshPublicKey"]["value"] == "REPLACE_WITH_SSH_PUBLIC_KEY":
        print("ERROR: No SSH public key found at ~/.ssh/id_rsa.pub or ~/.ssh/id_ed25519.pub")
        print("       Set sshPublicKey in infra/parameters.json or generate a key with: ssh-keygen")
        sys.exit(1)

    return params


def save_outputs(outputs):
    """Save deployment outputs to JSON file for use by other commands."""
    flat = {k: v.get("value", "") for k, v in outputs.items()}
    with open(OUTPUTS_FILE, "w") as f:
        json.dump(flat, f, indent=2)
    print(f"\n  Outputs saved to {OUTPUTS_FILE}")


def load_outputs():
    """Load deployment outputs from JSON file."""
    if not os.path.exists(OUTPUTS_FILE):
        print(f"No deployment outputs file found at {OUTPUTS_FILE}")
        print("Run 'uv run python deploy.py deploy' first.")
        sys.exit(1)
    with open(OUTPUTS_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_deploy():
    """Deploy all infrastructure."""
    print("=" * 60)
    print("DEPLOYING Aura BC Load Balancer POC")
    print("=" * 60)

    check_az_cli()
    params = load_params()

    print(f"\nAura BC FQDN: {params['auraFqdn']['value']}")
    print(f"Resource Group: {RESOURCE_GROUP}")
    print(f"Location: {LOCATION}")
    print()

    # Create resource group
    print("[1/3] Creating resource group...")
    run_az(f"group create --name {RESOURCE_GROUP} --location {LOCATION}")

    # Write temporary params file with resolved values
    resolved_params_path = os.path.join(os.path.dirname(__file__), "infra", ".params-resolved.json")
    resolved = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": params,
    }
    with open(resolved_params_path, "w") as f:
        json.dump(resolved, f, indent=2)

    # Deploy Bicep
    print("\n[2/3] Deploying Bicep template (this takes 3-5 minutes)...")
    result = run_az(
        f'deployment group create '
        f'--name {DEPLOYMENT_NAME} '
        f'--resource-group {RESOURCE_GROUP} '
        f'--template-file {BICEP_FILE} '
        f"--parameters @{resolved_params_path}"
    )

    # Clean up resolved params
    if os.path.exists(resolved_params_path):
        os.remove(resolved_params_path)

    if result is None:
        print("\nDeployment FAILED. Check errors above.")
        sys.exit(1)

    # Fetch and save outputs
    print("\n[3/3] Fetching deployment outputs...")
    outputs = run_az(
        f"deployment group show --resource-group {RESOURCE_GROUP} --name {DEPLOYMENT_NAME} "
        f"--query properties.outputs",
        check=False,
    )
    if outputs and isinstance(outputs, dict):
        save_outputs(outputs)
        print()
        for key, val in outputs.items():
            print(f"  {key}: {val.get('value', '')}")
    else:
        print("  WARNING: Could not fetch outputs. Run 'uv run python deploy.py outputs' to retry.")

    print("\n" + "=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print("\nNext steps:")
    print("  uv run python deploy.py allowlist   # Add NAT IP to Aura BC")
    print("  uv run python deploy.py status      # Verify everything is up")
    print("  uv run python deploy.py ssh          # SSH into proxy VM")


def cmd_status():
    """Show status of all deployed resources."""
    print("=" * 60)
    print("DEPLOYMENT STATUS")
    print("=" * 60)

    check_az_cli()

    # Check resource group
    exists = run_az(f"group exists --name {RESOURCE_GROUP}", parse_json=True)
    if not exists:
        print(f"\nResource group '{RESOURCE_GROUP}' does not exist. Nothing deployed.")
        return

    print(f"\nResource Group: {RESOURCE_GROUP}")

    # Deployment state
    print("\n--- Deployment ---")
    state = run_az(
        f"deployment group show --resource-group {RESOURCE_GROUP} --name {DEPLOYMENT_NAME} "
        f"--query properties.provisioningState --output tsv",
        check=False, parse_json=False,
    )
    print(f"  Provisioning State: {state or 'NOT FOUND'}")

    # VM status
    print("\n--- Proxy VM ---")
    vm_details = run_az(
        f"vm show --resource-group {RESOURCE_GROUP} --name {PREFIX}-proxy-vm --show-details",
        check=False,
    )
    if vm_details and isinstance(vm_details, dict):
        print(f"  Power State: {vm_details.get('powerState', 'unknown')}")
        print(f"  Private IP: {vm_details.get('privateIps', 'unknown')}")
    else:
        print("  VM not found")

    # NAT Gateway IP (from saved outputs or live query)
    print("\n--- NAT Gateway ---")
    saved_ip = None
    if os.path.exists(OUTPUTS_FILE):
        with open(OUTPUTS_FILE) as f:
            saved_ip = json.load(f).get("natGatewayPublicIp")
    ip = run_az(
        f"network public-ip show --resource-group {RESOURCE_GROUP} --name {PREFIX}-natgw-pip "
        f"--query ipAddress --output tsv",
        check=False, parse_json=False,
    ) or saved_ip
    print(f"  Public IP: {ip or 'NOT ALLOCATED'}")

    # Load Balancer
    print("\n--- Load Balancer ---")
    lb = run_az(
        f"network lb show --resource-group {RESOURCE_GROUP} --name {PREFIX}-ilb "
        f"--query provisioningState --output tsv",
        check=False, parse_json=False,
    )
    print(f"  Provisioning State: {lb or 'NOT FOUND'}")

    # Private Link Service
    print("\n--- Private Link Service ---")
    pls = run_az(
        f"network private-link-service show --resource-group {RESOURCE_GROUP} --name {PREFIX}-pls",
        check=False,
    )
    if pls and isinstance(pls, dict):
        print(f"  Provisioning State: {pls.get('provisioningState', 'unknown')}")
        print(f"  Resource ID: {pls.get('id', 'unknown')}")
        alias = pls.get("alias", "")
        if alias:
            print(f"  Alias: {alias}")
    else:
        print("  PLS not found")


def cmd_outputs():
    """Show deployment outputs and save to file."""
    check_az_cli()

    outputs = run_az(
        f"deployment group show --resource-group {RESOURCE_GROUP} --name {DEPLOYMENT_NAME} "
        f"--query properties.outputs",
        check=False,
    )
    if not outputs or not isinstance(outputs, dict):
        print("No deployment outputs found. Has the deployment completed?")
        return

    save_outputs(outputs)
    print()
    for key, val in outputs.items():
        print(f"  {key}: {val.get('value', '')}")


def cmd_allowlist():
    """Add the NAT Gateway IP to the Aura BC allowlist automatically."""
    outputs = load_outputs()
    nat_ip = outputs.get("natGatewayPublicIp", "")
    if not nat_ip:
        print("ERROR: No NAT Gateway IP found in deployment outputs.")
        sys.exit(1)

    print(f"NAT Gateway IP: {nat_ip}")
    print(f"Adding to Aura BC allowlist...")

    # Shell out to manage_ip_allowlist.py so we don't duplicate the Aura API logic
    manage_script = os.path.join(BASE_DIR, "manage_ip_allowlist.py")
    result = subprocess.run(
        [sys.executable, manage_script, "add", "--ip", nat_ip, "--description", "LB POC NAT Gateway"],
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        print("\nFailed to add IP to allowlist. Check Aura API credentials in .env")
        sys.exit(1)


def cmd_ssh():
    """SSH into the proxy VM."""
    check_az_cli()

    vm_details = run_az(
        f"vm show --resource-group {RESOURCE_GROUP} --name {PREFIX}-proxy-vm --show-details",
        check=False,
    )
    if not vm_details or not isinstance(vm_details, dict):
        print("Proxy VM not found. Deploy first.")
        return

    private_ip = vm_details.get("privateIps", "")
    if not private_ip:
        print("Could not determine VM private IP.")
        return

    print(f"VM private IP: {private_ip}")
    print("Attempting SSH via az ssh vm (tunnels through Azure control plane)...")
    print()

    os.execvp("az", [
        "az", "ssh", "vm",
        "--resource-group", RESOURCE_GROUP,
        "--name", f"{PREFIX}-proxy-vm",
    ])


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

    if os.path.exists(OUTPUTS_FILE):
        os.remove(OUTPUTS_FILE)

    print("Cleanup complete. All Azure resources deleted.")
    print("\nNOTE: If you added a NAT Gateway IP to the Aura BC allowlist, remove it:")
    print("  uv run python manage_ip_allowlist.py list")
    print("  uv run python manage_ip_allowlist.py remove --filter-id <ID>")


# ---------------------------------------------------------------------------
# Databricks API helpers
# ---------------------------------------------------------------------------

def get_databricks_token(profile=None):
    """Get a Databricks account admin token via CLI profile or .env."""
    if profile:
        print(f"  Using Databricks CLI profile: {profile}")
        result = subprocess.run(
            ["databricks", "auth", "token", "--profile", profile],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            print(f"  ERROR: Failed to get token from profile '{profile}'")
            print(f"  {result.stderr.strip()}")
            sys.exit(1)
        try:
            return json.loads(result.stdout)["access_token"]
        except (json.JSONDecodeError, KeyError):
            print(f"  ERROR: Unexpected output from databricks auth token: {result.stdout}")
            sys.exit(1)

    token = os.getenv("DATABRICKS_ACCOUNT_TOKEN", "")
    if not token:
        import getpass
        token = getpass.getpass("Databricks account admin token: ")
    if not token:
        print("ERROR: Token is required.")
        sys.exit(1)
    return token


def databricks_api(method, url, token, data=None):
    """Make an authenticated request to the Databricks Account API."""
    cmd = [
        "curl", "--silent", "--show-error", "--location",
        "--request", method, url,
        "--header", "Content-Type: application/json",
        "--header", f"Authorization: Bearer {token}",
    ]
    if data is not None:
        cmd.extend(["--data", json.dumps(data)])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        sys.exit(1)

    if not result.stdout.strip():
        return {}

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  ERROR: Unexpected response: {result.stdout}")
        sys.exit(1)

    if "error_code" in response or "error" in response:
        error = response.get("message", response.get("error", result.stdout))
        print(f"  ERROR: {error}")
        sys.exit(1)

    return response


def require_env(name, hint=""):
    """Get a required environment variable or exit."""
    value = os.getenv(name, "")
    if not value:
        msg = f"ERROR: {name} is required."
        if hint:
            msg += f" {hint}"
        print(msg)
        sys.exit(1)
    return value


def update_env_file(key, value):
    """Add or update a key=value pair in the .env file."""
    env_path = os.path.join(BASE_DIR, ".env")
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}=") or line.strip() == f"{key}=":
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)


def parse_profile_arg():
    """Extract --profile value from sys.argv (after the subcommand)."""
    args = sys.argv[2:]  # skip script name and subcommand
    for i, arg in enumerate(args):
        if arg == "--profile" and i + 1 < len(args):
            return args[i + 1]
    return None


# ---------------------------------------------------------------------------
# Databricks NCC commands
# ---------------------------------------------------------------------------

def cmd_create_ncc():
    """Create a Databricks Network Connectivity Configuration."""
    print("=" * 60)
    print("CREATE DATABRICKS NCC")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    ncc_name = f"{PREFIX}-ncc"
    # NCC must be in the same region as the Databricks workspace, which may
    # differ from the Azure infrastructure region (AZURE_LOCATION).
    region = os.getenv("NCC_REGION", LOCATION)

    print(f"\n  Account ID: {account_id}")
    print(f"  NCC name:   {ncc_name}")
    print(f"  Region:     {region}")

    url = f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs"
    response = databricks_api("POST", url, token, {"name": ncc_name, "region": region})

    ncc_id = response.get("network_connectivity_config_id", "")
    if not ncc_id:
        print("  ERROR: NCC created but no ID returned.")
        print(f"  Response: {json.dumps(response, indent=2)}")
        sys.exit(1)

    # Save NCC_ID to .env
    update_env_file("NCC_ID", ncc_id)
    # Also set it in the current process so subsequent commands can use it
    os.environ["NCC_ID"] = ncc_id

    print(f"\n  NCC ID: {ncc_id}")
    print(f"  Saved to .env as NCC_ID")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print("\nNext step:")
    print("  uv run python deploy.py create-pe-rule   # Add private endpoint rule")


def cmd_create_pe_rule():
    """Create a private endpoint rule in the NCC."""
    print("=" * 60)
    print("CREATE PRIVATE ENDPOINT RULE")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Run 'create-ncc' first, or set NCC_ID in .env")
    domain = os.getenv("NEO4J_DOMAIN", "")
    if not domain:
        # Extract FQDN from NEO4J_URI (strip scheme prefix)
        uri = os.getenv("NEO4J_URI", "")
        for prefix in ("neo4j+s://", "bolt+s://", "neo4j://", "bolt://"):
            if uri.startswith(prefix):
                domain = uri[len(prefix):].split(":")[0].split("/")[0]
                break
    if not domain:
        print("ERROR: NEO4J_DOMAIN is required. Set it to the real Aura FQDN (e.g. f5919d06.databases.neo4j.io).")
        sys.exit(1)
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    # Get PLS resource ID from deployment outputs
    outputs = load_outputs()
    pls_id = outputs.get("privateLinkServiceId", "")
    if not pls_id:
        print("ERROR: No privateLinkServiceId in deployment outputs.")
        print("Run 'uv run python deploy.py deploy' first.")
        sys.exit(1)

    print(f"\n  Account ID: {account_id}")
    print(f"  NCC ID:     {ncc_id}")
    print(f"  PLS ID:     {pls_id}")
    print(f"  Domain:     {domain}")

    url = (
        f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs/"
        f"{ncc_id}/private-endpoint-rules"
    )
    # For Private Link Service resources, the Databricks API does not require
    # group_id. Only resource_id and domain_names are needed.
    # See: https://learn.microsoft.com/azure/databricks/security/network/serverless-network-security/pl-to-internal-network
    response = databricks_api("POST", url, token, {
        "resource_id": pls_id,
        "domain_names": [domain],
    })

    rule_id = response.get("rule_id", "")
    status = response.get("connection_state", "")

    print(f"\n  Rule ID: {rule_id}")
    print(f"  Status:  {status}")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print("\nThe private endpoint will appear as a pending connection on your PLS.")
    print("Next step:")
    print("  uv run python deploy.py approve   # Approve the pending connection")


def cmd_approve():
    """Approve pending private endpoint connections on the PLS."""
    print("=" * 60)
    print("APPROVE PRIVATE LINK CONNECTIONS")
    print("=" * 60)

    check_az_cli()
    pls_name = f"{PREFIX}-pls"

    print(f"\nChecking {pls_name} for private endpoint connections...")

    # Get PLS and its connections
    pls = run_az(
        f"network private-link-service show "
        f"--resource-group {RESOURCE_GROUP} --name {pls_name} "
        f"--query privateEndpointConnections",
        check=False,
    )

    if not pls or not isinstance(pls, list):
        print("\n  No private endpoint connections found.")
        print("  Create an NCC private endpoint rule first:")
        print("    uv run python deploy.py create-pe-rule")
        return

    # Parse connections
    pending = []
    for conn in pls:
        state = conn.get("privateLinkServiceConnectionState", {})
        name = conn.get("name", "")
        status = state.get("status", "")
        description = state.get("description", "")
        print(f"\n  {name}")
        print(f"    Status:      {status}")
        print(f"    Description: {description}")
        if status == "Pending":
            pending.append(name)

    if not pending:
        approved = [c for c in pls
                    if c.get("privateLinkServiceConnectionState", {}).get("status") == "Approved"]
        print(f"\n  No pending connections. {len(approved)} already approved.")
        return

    # Approve pending connections
    print(f"\nApproving {len(pending)} pending connection(s)...")
    for conn_name in pending:
        print(f"\n  Approving: {conn_name}")
        run_az(
            f"network private-link-service connection update "
            f"--resource-group {RESOURCE_GROUP} "
            f"--service-name {pls_name} "
            f'--name {conn_name} '
            f'--connection-status Approved '
            f'--description "Approved for Databricks serverless"',
        )
        print("    Approved.")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print("Wait for the NCC status in Databricks to show ESTABLISHED")
    print("(up to 10 minutes), then:")
    print("  uv run python deploy.py attach-ncc --profile <profile>")


def cmd_attach_ncc():
    """Attach the NCC to a Databricks workspace."""
    print("=" * 60)
    print("ATTACH NCC TO DATABRICKS WORKSPACE")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Run 'create-ncc' first, or set NCC_ID in .env")
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    print(f"\n  Account ID: {account_id}")
    print(f"  NCC ID:     {ncc_id}")

    workspace_id = os.getenv("DATABRICKS_WORKSPACE_ID", "")
    if not workspace_id:
        workspace_id = input("\nDatabricks workspace ID: ").strip()
    if not workspace_id:
        print("ERROR: Workspace ID is required.")
        sys.exit(1)

    print(f"\nAttaching NCC {ncc_id} to workspace {workspace_id}...")

    url = (
        f"{DATABRICKS_BASE_URL}/{account_id}/workspaces/{workspace_id}"
    )
    response = databricks_api("PATCH", url, token, {
        "network_connectivity_config_id": ncc_id,
    })

    workspace_name = response.get("workspace_name", workspace_id)

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  Workspace: {workspace_name}")
    print(f"  NCC:       {ncc_id}")
    print()
    print("It may take a few minutes for serverless compute to pick up")
    print("the NCC. Next step:")
    print("  uv run python deploy.py setup-secrets --profile <profile>")


def cmd_setup_secrets():
    """Store Neo4j credentials in a Databricks secret scope."""
    print("=" * 60)
    print("SETUP DATABRICKS SECRETS")
    print("=" * 60)

    profile = parse_profile_arg()
    dbx_prefix = ["databricks"]
    if profile:
        dbx_prefix.extend(["--profile", profile])

    neo4j_password = os.getenv("NEO4J_PASSWORD", "")
    if not neo4j_password:
        print("ERROR: NEO4J_PASSWORD not set in .env")
        sys.exit(1)

    scope_name = "neo4j-aurabc-lb"

    print(f"\n  Profile:    {profile or 'DEFAULT'}")
    print(f"  Scope:      {scope_name}")

    # Create secret scope (ignore error if already exists)
    print(f"\n  Creating secret scope: {scope_name}")
    result = subprocess.run(
        [*dbx_prefix, "secrets", "create-scope", scope_name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        print("    Created.")
    else:
        if "RESOURCE_ALREADY_EXISTS" in result.stderr or "already exists" in result.stderr:
            print("    Already exists.")
        else:
            print(f"    Warning: {result.stderr.strip()}")

    # Store password
    print(f"  Storing secret: password")
    result = subprocess.run(
        [*dbx_prefix, "secrets", "put-secret", scope_name, "password",
         "--string-value", neo4j_password],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr.strip()}")
        sys.exit(1)
    print("    Stored.")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  Secrets stored in scope: {scope_name}")
    print()
    print("Use in Databricks notebooks:")
    print(f'  password = dbutils.secrets.get("{scope_name}", "password")')
    print()
    print("Next step: Import aurabc_private_link_test.ipynb and run on serverless compute.")


def cmd_detach_ncc():
    """Detach NCC from workspace, delete rules, delete NCC."""
    print("=" * 60)
    print("DETACH NCC AND REMOVE PRIVATE ENDPOINT RULES")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Set NCC_ID in .env")
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    print(f"\n  Account ID: {account_id}")
    print(f"  NCC ID:     {ncc_id}")

    workspace_id = os.getenv("DATABRICKS_WORKSPACE_ID", "")
    if not workspace_id:
        workspace_id = input("\nDatabricks workspace ID: ").strip()
    if not workspace_id:
        print("ERROR: Workspace ID is required.")
        sys.exit(1)

    # Step 1: Get workspace and NCC details
    print("\nStep 1: Get workspace and NCC details")
    workspace_url = f"{DATABRICKS_BASE_URL}/{account_id}/workspaces/{workspace_id}"
    workspace = databricks_api("GET", workspace_url, token)
    workspace_name = workspace.get("workspace_name", workspace_id)
    workspace_region = workspace.get("location", workspace.get("azure_workspace_info", {}).get("region", ""))
    current_ncc = workspace.get("network_connectivity_config_id", "")
    print(f"  Workspace:   {workspace_name}")
    print(f"  Region:      {workspace_region}")
    print(f"  Current NCC: {current_ncc}")

    if current_ncc and current_ncc != ncc_id:
        print(f"\n  WARNING: Workspace is attached to NCC {current_ncc},")
        print(f"  not the NCC in .env ({ncc_id}).")
        choice = input("  Continue anyway? [y/N]: ").strip().lower()
        if choice != "y":
            print("Cancelled.")
            return

    ncc_url = f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs/{ncc_id}"
    ncc = databricks_api("GET", ncc_url, token)
    ncc_name = ncc.get("name", ncc_id)
    ncc_region = ncc.get("region", "")
    print(f"  NCC name:    {ncc_name}")
    print(f"  NCC region:  {ncc_region}")

    # Step 2: Detach NCC from workspace
    print(f"\nStep 2: Detach NCC from workspace")
    swapped_to_placeholder = False
    if current_ncc == ncc_id:
        region = ncc_region or workspace_region
        if not region:
            region = input("  Azure region for placeholder NCC: ").strip()
            if not region:
                print("ERROR: Region is required.")
                sys.exit(1)

        # Find or create placeholder
        list_url = f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs"
        all_nccs = databricks_api("GET", list_url, token).get("items", [])
        placeholder_id = ""
        for n in all_nccs:
            if n.get("name") == NCC_PLACEHOLDER_NAME and n.get("region") == region:
                placeholder_id = n.get("network_connectivity_config_id", "")
                if placeholder_id:
                    print(f"  Reusing existing placeholder NCC: {placeholder_id}")
                    break

        if not placeholder_id:
            print(f"  Creating placeholder NCC in {region}...")
            placeholder = databricks_api("POST", list_url, token, {
                "name": NCC_PLACEHOLDER_NAME, "region": region,
            })
            placeholder_id = placeholder.get("network_connectivity_config_id", "")
            print(f"  Created placeholder NCC: {placeholder_id}")

        print("  Swapping workspace to placeholder NCC...")
        databricks_api("PATCH", workspace_url, token, {
            "network_connectivity_config_id": placeholder_id,
        })
        print("  Workspace now uses placeholder NCC.")
        swapped_to_placeholder = True
    elif not current_ncc:
        print("  Workspace has no NCC attached. Skipping detach.")
    else:
        print(f"  Workspace uses a different NCC ({current_ncc}). Skipping detach.")

    # Step 3: Delete private endpoint rules
    print(f"\nStep 3: Delete private endpoint rules from NCC")
    rules_url = (
        f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs/"
        f"{ncc_id}/private-endpoint-rules"
    )
    rules_response = databricks_api("GET", rules_url, token)
    rules = rules_response.get("items", [])
    had_established = False

    if not rules:
        print("  No private endpoint rules found.")
    else:
        print(f"  Found {len(rules)} rule(s):")
        for rule in rules:
            rule_id = rule.get("rule_id", "")
            status = rule.get("connection_state", "")
            if status in ("ESTABLISHED", "REJECTED", "DISCONNECTED"):
                had_established = True
            print(f"    {rule_id} ({status})")
            delete_url = f"{rules_url}/{rule_id}"
            databricks_api("DELETE", delete_url, token)
            print(f"    Deleted.")

    # Step 4: Delete the NCC
    print(f"\nStep 4: Delete NCC ({ncc_name})")
    databricks_api("DELETE", ncc_url, token)
    print("  NCC deleted.")

    # Clear NCC_ID from .env
    update_env_file("NCC_ID", "")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  NCC {ncc_name} has been removed from workspace {workspace_name}")
    print(f"  and deleted along with its private endpoint rules.")
    print()

    if had_established:
        print("  Note: Rules that were in ESTABLISHED, REJECTED, or DISCONNECTED")
        print("  state may be retained by Databricks for up to 7 days before")
        print("  the private endpoint is permanently removed from your Azure")
        print("  resource.")
        print()

    if swapped_to_placeholder:
        print("  A placeholder NCC (neo4j-ncc-placeholder) was attached to the")
        print("  workspace. You can leave it in place or remove it from the")
        print("  account console if no longer needed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manage the Aura BC Load Balancer POC infrastructure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands (Azure infrastructure):
  deploy         Deploy all Azure infrastructure
  allowlist      Add NAT Gateway IP to Aura BC allowlist
  status         Show status of deployed resources
  outputs        Show deployment outputs (NAT IP, PLS ID, etc.)
  ssh            SSH into the proxy VM
  cleanup        Tear down all infrastructure

Commands (Databricks NCC):
  create-ncc     Create a Databricks NCC
  create-pe-rule Create private endpoint rule in NCC
  approve        Approve pending PLS connections
  attach-ncc     Attach NCC to a Databricks workspace
  setup-secrets  Store Neo4j credentials in Databricks secrets
  detach-ncc     Detach and delete NCC from Databricks

Databricks commands accept --profile <name> for CLI authentication.
        """,
    )
    all_commands = [
        "deploy", "allowlist", "status", "outputs", "ssh", "cleanup",
        "create-ncc", "create-pe-rule", "approve", "attach-ncc",
        "setup-secrets", "detach-ncc",
    ]
    parser.add_argument("command", choices=all_commands)
    # Allow extra args (--profile) to pass through
    args, _ = parser.parse_known_args()

    commands = {
        "deploy": cmd_deploy,
        "allowlist": cmd_allowlist,
        "status": cmd_status,
        "outputs": cmd_outputs,
        "ssh": cmd_ssh,
        "cleanup": cmd_cleanup,
        "create-ncc": cmd_create_ncc,
        "create-pe-rule": cmd_create_pe_rule,
        "approve": cmd_approve,
        "attach-ncc": cmd_attach_ncc,
        "setup-secrets": cmd_setup_secrets,
        "detach-ncc": cmd_detach_ncc,
    }

    commands[args.command]()


if __name__ == "__main__":
    main()
