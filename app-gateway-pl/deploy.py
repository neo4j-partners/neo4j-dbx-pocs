"""
Databricks NCC integration and Azure resource management for the Application Gateway POC.

Azure setup is handled by setup_azure.py. This script manages the Databricks
side and post-deployment operations.

Commands:
    uv run python deploy.py status        — Show status of deployed resources
    uv run python deploy.py create-ncc    — Create a Databricks NCC
    uv run python deploy.py create-pe-rule — Create private endpoint rule in NCC
    uv run python deploy.py approve       — Approve pending private endpoint connections
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
RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP", "aurabc-appgw-poc-rg")
LOCATION = os.getenv("AZURE_LOCATION", "eastus")
PREFIX = os.getenv("AZURE_PREFIX", "aurabc-appgw")
RESOURCES_FILE = os.path.join(BASE_DIR, "azure-resources.json")

DATABRICKS_BASE_URL = "https://accounts.azuredatabricks.net/api/2.0/accounts"
NCC_PLACEHOLDER_NAME = "neo4j-ncc-placeholder"


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


def load_resources():
    """Load the resource manifest written by setup_azure.py."""
    if not os.path.exists(RESOURCES_FILE):
        print(f"No resource manifest found at {RESOURCES_FILE}")
        print("Run 'uv run python setup_azure.py' first.")
        sys.exit(1)
    with open(RESOURCES_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status():
    """Show status of all deployed resources."""
    print("=" * 60)
    print("DEPLOYMENT STATUS")
    print("=" * 60)

    check_az_cli()

    exists = run_az(f"group exists --name {RESOURCE_GROUP}", parse_json=True)
    if not exists:
        print(f"\nResource group '{RESOURCE_GROUP}' does not exist. Nothing deployed.")
        return

    print(f"\nResource Group: {RESOURCE_GROUP}")

    # Load saved resource manifest for context
    if os.path.exists(RESOURCES_FILE):
        resources = json.load(open(RESOURCES_FILE))
        deployed_at = resources.get("metadata", {}).get("deployedAt", "unknown")
        print(f"Deployed at:    {deployed_at}")

    # Application Gateway status
    print("\n--- Application Gateway ---")
    appgw = run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw",
        check=False,
    )
    if appgw and isinstance(appgw, dict):
        print(f"  Provisioning State: {appgw.get('provisioningState', 'unknown')}")
        print(f"  Operational State:  {appgw.get('operationalState', 'unknown')}")
        print(f"  Resource ID:        {appgw.get('id', 'unknown')}")

        # Check listeners
        listeners = appgw.get("listeners", [])
        if listeners:
            print(f"  TCP Listeners:      {len(listeners)}")
            for l in listeners:
                proto = l.get("properties", l).get("protocol", l.get("protocol", "unknown"))
                print(f"    - {l.get('name', 'unnamed')} ({proto})")

        # Check backend pools
        pools = appgw.get("backendAddressPools", [])
        if pools:
            for pool in pools:
                props = pool.get("properties", pool)
                addresses = props.get("backendAddresses", [])
                print(f"  Backend Pool:       {pool.get('name', 'unnamed')}")
                for addr in addresses:
                    print(f"    - {addr.get('fqdn', addr.get('ipAddress', 'unknown'))}")

        # Check Private Link configurations
        pl_configs = appgw.get("privateLinkConfigurations", [])
        if pl_configs:
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

    # Backend health
    print("\n--- Backend Health ---")
    print("  (Checking backend health — this may take a moment...)")
    health = run_az(
        f"network application-gateway show-backend-health "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw",
        check=False,
    )
    if health and isinstance(health, dict):
        for pool in health.get("backendAddressPools", []):
            pool_name = pool.get("backendAddressPool", {}).get("id", "").split("/")[-1]
            print(f"  Pool: {pool_name}")
            for setting in pool.get("backendSettings", pool.get("backendHttpSettingsCollection", [])):
                servers = setting.get("servers", [])
                for server in servers:
                    addr = server.get("address", "unknown")
                    server_health = server.get("health", "unknown")
                    print(f"    {addr}: {server_health}")
    else:
        print("  Could not retrieve backend health (gateway may still be provisioning)")

    # Private endpoint connections
    print("\n--- Private Endpoint Connections ---")
    pe_conns = run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {PREFIX}-gw "
        f"--query privateEndpointConnections",
        check=False,
    )
    if pe_conns and isinstance(pe_conns, list) and len(pe_conns) > 0:
        for conn in pe_conns:
            name = conn.get("name", "unnamed")
            props = conn.get("properties", conn)
            link_state = props.get("privateLinkServiceConnectionState", {})
            status = link_state.get("status", "unknown")
            print(f"  {name}: {status}")
    else:
        print("  No private endpoint connections")


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
    args = sys.argv[2:]
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
    region = os.getenv("NCC_REGION", "") or LOCATION

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

    update_env_file("NCC_ID", ncc_id)
    os.environ["NCC_ID"] = ncc_id

    print(f"\n  NCC ID: {ncc_id}")
    print(f"  Saved to .env as NCC_ID")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print("\nNext step:")
    print("  uv run python deploy.py create-pe-rule   # Add private endpoint rule")


def cmd_create_pe_rule():
    """Create a private endpoint rule in the NCC.

    For Application Gateway, the Databricks API requires:
    - resource_id: the Application Gateway resource ID
    - group_id: the Private Link configuration name
    - domain_names: the Neo4j domain for DNS resolution
    """
    print("=" * 60)
    print("CREATE PRIVATE ENDPOINT RULE")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Run 'create-ncc' first, or set NCC_ID in .env")
    domain = require_env("NEO4J_DOMAIN", "Must be the real Aura FQDN (e.g. xxxxxxxx.databases.neo4j.io). Used as TLS SNI hostname.")
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    # Get Application Gateway resource ID and Private Link config name from resource manifest
    resources = load_resources()
    appgw = resources.get("applicationGateway", {})
    appgw_resource_id = appgw.get("resourceId", "")
    frontend_ip_config_name = appgw.get("frontendIpConfigName", "")

    if not appgw_resource_id:
        print("ERROR: No applicationGateway.resourceId in azure-resources.json.")
        print("Run 'uv run python setup_azure.py' first.")
        sys.exit(1)

    if not frontend_ip_config_name:
        print("ERROR: No applicationGateway.frontendIpConfigName in azure-resources.json.")
        print("  Re-run 'uv run python setup_azure.py phase1' to regenerate azure-resources.json")
        sys.exit(1)

    print(f"\n  Account ID:    {account_id}")
    print(f"  NCC ID:        {ncc_id}")
    print(f"  App GW ID:     {appgw_resource_id}")
    print(f"  Group ID:      {frontend_ip_config_name}")
    print(f"  Domain:        {domain}")

    url = (
        f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs/"
        f"{ncc_id}/private-endpoint-rules"
    )
    response = databricks_api("POST", url, token, {
        "resource_id": appgw_resource_id,
        "group_id": frontend_ip_config_name,
        "domain_names": [domain],
    })

    rule_id = response.get("rule_id", "")
    status = response.get("connection_state", "")

    print(f"\n  Rule ID: {rule_id}")
    print(f"  Status:  {status}")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print("\nThe private endpoint will appear as a pending connection on the Application Gateway.")
    print("Next step:")
    print("  uv run python deploy.py approve   # Approve the pending connection")


def cmd_approve():
    """Approve pending private endpoint connections on the Application Gateway."""
    print("=" * 60)
    print("APPROVE PRIVATE ENDPOINT CONNECTIONS")
    print("=" * 60)

    check_az_cli()
    appgw_name = f"{PREFIX}-gw"

    print(f"\nChecking {appgw_name} for private endpoint connections...")

    pe_conns = run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {appgw_name} "
        f"--query privateEndpointConnections",
        check=False,
    )

    if not pe_conns or not isinstance(pe_conns, list):
        print("\n  No private endpoint connections found.")
        print("  Create an NCC private endpoint rule first:")
        print("    uv run python deploy.py create-pe-rule")
        return

    pending = []
    for conn in pe_conns:
        props = conn.get("properties", conn)
        state = props.get("privateLinkServiceConnectionState", {})
        name = conn.get("name", "")
        status = state.get("status", "")
        description = state.get("description", "")
        print(f"\n  {name}")
        print(f"    Status:      {status}")
        print(f"    Description: {description}")
        if status == "Pending":
            pending.append(conn.get("id", ""))

    if not pending:
        approved = [c for c in pe_conns
                    if c.get("properties", c).get("privateLinkServiceConnectionState", {}).get("status") == "Approved"]
        print(f"\n  No pending connections. {len(approved)} already approved.")
        return

    print(f"\nApproving {len(pending)} pending connection(s)...")
    for conn_id in pending:
        conn_name = conn_id.split("/")[-1]
        print(f"\n  Approving: {conn_name}")
        body = json.dumps({
            "properties": {
                "privateLinkServiceConnectionState": {
                    "status": "Approved",
                    "description": "Approved for Databricks serverless"
                }
            }
        })
        run_az(
            f'rest --method PUT '
            f'--url "https://management.azure.com{conn_id}?api-version=2025-01-01" '
            f"--body '{body}'",
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

    url = f"{DATABRICKS_BASE_URL}/{account_id}/workspaces/{workspace_id}"
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

    scope_name = "neo4j-appgw-poc"

    print(f"\n  Profile:    {profile or 'DEFAULT'}")
    print(f"  Scope:      {scope_name}")

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


def cmd_ncc_status():
    """Show NCC, PE rule, and workspace attachment status."""
    print("=" * 60)
    print("NCC STATUS")
    print("=" * 60)

    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Run 'create-ncc' first, or set NCC_ID in .env")
    profile = parse_profile_arg()
    token = get_databricks_token(profile)

    # NCC details
    ncc_url = f"{DATABRICKS_BASE_URL}/{account_id}/network-connectivity-configs/{ncc_id}"
    ncc = databricks_api("GET", ncc_url, token)

    print(f"\n--- NCC ---")
    print(f"  Name:   {ncc.get('name', 'unknown')}")
    print(f"  ID:     {ncc.get('network_connectivity_config_id', 'unknown')}")
    print(f"  Region: {ncc.get('region', 'unknown')}")

    # PE rules
    rules = (
        ncc.get("egress_config", {})
        .get("target_rules", {})
        .get("azure_private_endpoint_rules", [])
    )
    print(f"\n--- Private Endpoint Rules ({len(rules)}) ---")
    for rule in rules:
        state = rule.get("connection_state", "unknown")
        domain = ", ".join(rule.get("domain_names", []))
        group_id = rule.get("group_id", "unknown")
        rule_id = rule.get("rule_id", "unknown")
        resource = rule.get("resource_id", "").split("/")[-1]
        print(f"  Rule:     {rule_id}")
        print(f"  State:    {state}")
        print(f"  Domain:   {domain}")
        print(f"  Group ID: {group_id}")
        print(f"  Resource: {resource}")
        if len(rules) > 1:
            print()

    # Workspace attachment
    workspace_id = os.getenv("DATABRICKS_WORKSPACE_ID", "")
    if workspace_id:
        print(f"\n--- Workspace ---")
        workspace_url = f"{DATABRICKS_BASE_URL}/{account_id}/workspaces/{workspace_id}"
        workspace = databricks_api("GET", workspace_url, token)
        ws_ncc = workspace.get("network_connectivity_config_id", "")
        print(f"  Name:       {workspace.get('workspace_name', 'unknown')}")
        print(f"  ID:         {workspace_id}")
        print(f"  NCC:        {ws_ncc or 'NONE'}")
        print(f"  Attached:   {'YES' if ws_ncc == ncc_id else 'NO' if ws_ncc else 'NO NCC'}")

    # Azure PE connections
    print(f"\n--- App Gateway PE Connections ---")
    check_az_cli()
    appgw_name = f"{PREFIX}-gw"
    pe_conns = run_az(
        f"network application-gateway show "
        f"--resource-group {RESOURCE_GROUP} --name {appgw_name} "
        f"--query privateEndpointConnections",
        check=False,
    )
    if pe_conns and isinstance(pe_conns, list):
        for conn in pe_conns:
            props = conn.get("properties", conn)
            state = props.get("privateLinkServiceConnectionState", {})
            name = conn.get("name", "unknown")
            status = state.get("status", "unknown")
            print(f"  {name}: {status}")
    else:
        print("  No PE connections")


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
        description="Databricks NCC integration for the Application Gateway POC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Azure setup:
  Run 'uv run python setup_azure.py' to deploy all Azure infrastructure.

Commands (post-deployment):
  status         Show status of deployed resources
  cleanup        Tear down all Azure infrastructure

Commands (Databricks NCC):
  create-ncc     Create a Databricks NCC
  create-pe-rule Create private endpoint rule in NCC
  approve        Approve pending private endpoint connections
  attach-ncc     Attach NCC to a Databricks workspace
  setup-secrets  Store Neo4j credentials in Databricks secrets
  ncc-status     Show NCC, PE rule, and workspace status
  detach-ncc     Detach and delete NCC from Databricks

Databricks commands accept --profile <name> for CLI authentication.
        """,
    )
    all_commands = [
        "status", "cleanup",
        "create-ncc", "create-pe-rule", "approve", "attach-ncc",
        "setup-secrets", "ncc-status", "detach-ncc",
    ]
    parser.add_argument("command", choices=all_commands)
    args, _ = parser.parse_known_args()

    commands = {
        "status": cmd_status,
        "cleanup": cmd_cleanup,
        "create-ncc": cmd_create_ncc,
        "create-pe-rule": cmd_create_pe_rule,
        "approve": cmd_approve,
        "attach-ncc": cmd_attach_ncc,
        "setup-secrets": cmd_setup_secrets,
        "ncc-status": cmd_ncc_status,
        "detach-ncc": cmd_detach_ncc,
    }

    commands[args.command]()


if __name__ == "__main__":
    main()
