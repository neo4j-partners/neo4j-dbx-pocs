# Aura BC Load Balancer POC

Connects Databricks serverless compute to Neo4j Aura Business Critical over Azure Private Link using a load balancer and reverse proxy architecture. Validated end-to-end.

## Architecture

Databricks serverless cannot create outbound private endpoints directly to external services. This project works around that limitation by deploying an Azure Private Link Service backed by an internal load balancer and an HAProxy VM that forwards Bolt traffic to Aura BC over the public internet via a NAT Gateway with a static IP.

```
Databricks Serverless (eastus)
    |
    |  NCC private endpoint
    v
Private Link Service (eastus)
    |
    v
Internal Load Balancer
    |  port 7687
    v
HAProxy VM  [TCP passthrough, no TLS termination]
    |
    |  NAT Gateway (static public IP, allowlisted on Aura BC)
    v
Neo4j Aura BC (eastus, xxxxxxxx.databases.neo4j.io:7687)
```

All resources are deployed in the same region (eastus) as the Databricks workspace and the Aura BC instance.

## Why the proxy VM is necessary

Azure Private Link Service requires an Internal Load Balancer as its backend, and an Internal Load Balancer can only route to private IPs within its own VNet — it cannot forward traffic to an external public endpoint. Aura BC is a managed service that lives outside your Azure network, so there is no private IP to put in the load balancer's backend pool.

The HAProxy VM solves this by giving the load balancer a local target inside the VNet. The LB forwards to HAProxy, and HAProxy forwards out to Aura BC over the public internet. It is a simple TCP passthrough — no TLS termination, no protocol translation, just a hop that bridges the private and public network boundary.

**Downsides:**

- A VM you have to run and maintain (patching, monitoring, availability).
- Traffic from the proxy to Aura BC still traverses the public internet over TLS — the path is not fully private end-to-end. Only Neo4j Aura VDC with native Azure Private Link would eliminate this public hop.
- Added latency from the extra network hop through the proxy.
- NAT Gateway is required so the proxy's outbound IP is static and can be allowlisted on Aura BC.

## How it works

- **Network Connectivity Configuration (NCC):** A Databricks account-level resource that controls how serverless compute makes outbound network connections. The NCC must be created in the same region as the Databricks workspace. Set `NCC_REGION` in `.env` if these differ.

- **Private endpoint rule:** An NCC rule that tells Databricks to route traffic for a specific domain through a private endpoint to a Private Link Service. The domain must be the real Aura BC FQDN (e.g. `f5919d06.databases.neo4j.io`) because Databricks uses it as the TLS SNI hostname and Aura BC rejects connections with unrecognized SNI values.

- **Private Link Service (PLS):** An Azure resource that accepts private endpoint connections and forwards traffic to a load balancer. Connections from the NCC private endpoint are auto-approved or can be approved manually.

- **Internal Load Balancer:** Receives traffic from the PLS on port 7687 and distributes it to the HAProxy backend pool.

- **HAProxy VM:** Runs in TCP passthrough mode with no TLS termination. Receives Bolt connections from the load balancer and forwards them to the Aura BC FQDN. The `check` health check directive must be disabled because plain TCP probes without TLS cause Aura BC to drop connections.

- **NAT Gateway:** Provides a static public IP for all outbound traffic from the HAProxy VM. This IP is added to the Aura BC IP allowlist so that Aura accepts connections from the proxy.

- **bolt+s:// protocol:** The Neo4j driver must use `bolt+s://` instead of `neo4j+s://`. The `neo4j+s://` scheme performs routing table discovery, which returns the Aura FQDN and causes the driver to bypass the private endpoint entirely.

- **Keepalive settings:** Azure Private Link Service enforces an idle timeout of approximately 300 seconds. The Neo4j driver must set `max_connection_lifetime` and `liveness_check_timeout` to values below 300 seconds to avoid silent connection drops.

## Quick Start

### Prerequisites

- Azure CLI installed (`brew install azure-cli`)
- `uv` installed (`brew install uv`)
- Databricks CLI installed (`brew install databricks`)
- SSH key at `~/.ssh/id_rsa.pub` or `~/.ssh/id_ed25519.pub`
- Neo4j Aura BC instance with API credentials
- Databricks account admin permissions (for NCC management)

### Databricks CLI Profile Setup

The Databricks NCC commands use a CLI profile for authentication. Set up a profile in `~/.databrickscfg`:

```ini
[azure-account-admin]
host       = https://accounts.azuredatabricks.net
account_id = <your-azure-account-id>
auth_type  = databricks-cli
```

Then authenticate (opens a browser for OAuth login):

```bash
databricks auth login --profile azure-account-admin
```

### 1. Log in to Azure

```bash
az login
az account show   # verify correct subscription
```

You only need `az` (Azure CLI). `azd` (Azure Developer CLI) is not used.

### 2. Configure credentials

```bash
cp .env.sample .env
# Edit .env with your values
```

Required variables:

| Variable | Purpose | Where to find it |
|----------|---------|-------------------|
| `NEO4J_URI` | Aura BC connection URI | Aura Console > Instance > Connect |
| `NEO4J_USERNAME` | Database username | Aura Console |
| `NEO4J_PASSWORD` | Database password | Set at instance creation |
| `AURA_API_CLIENT_ID` | API key ID | Aura Console > Account > API Credentials |
| `AURA_API_CLIENT_SECRET` | API key secret | Shown once at creation |
| `AURA_ORG_ID` | Organization ID | Aura Console URL or Organization Settings |
| `AURA_INSTANCE_ID` | Instance ID | Aura Console > Instance details |
| `DATABRICKS_ACCOUNT_ID` | Databricks account UUID | Databricks Account Console URL |
| `DATABRICKS_WORKSPACE_ID` | Workspace numeric ID | Databricks Workspace URL |
| `NCC_REGION` | Region for the NCC (must match workspace region). Defaults to `AZURE_LOCATION` if unset. | Databricks Workspace settings or URL |
| `NEO4J_DOMAIN` | Domain for NCC PE rule. Must be the real Aura FQDN (e.g. `f5919d06.databases.neo4j.io`). | Aura Console > Instance > Connect |

### 3. Deploy Azure infrastructure

```bash
uv run python deploy.py deploy      # creates all Azure infra (~3-5 min)
uv run python deploy.py allowlist   # adds NAT IP to Aura BC (automatic)
uv run python deploy.py status      # verify everything is up
```

The `deploy` command saves all outputs (NAT IP, PLS ID, etc.) to `deployment-outputs.json`, and subsequent commands read from it automatically. No copy-pasting IPs.

### 4. Connect to Databricks

> **Important:** The NCC must be in the same Azure region as the Databricks workspace. Set `NCC_REGION` in `.env` if your workspace is in a different region than the Azure infrastructure.

```bash
uv run python deploy.py create-ncc --profile azure-account-admin      # create NCC
uv run python deploy.py create-pe-rule --profile azure-account-admin  # add PE rule
uv run python deploy.py approve                                       # approve connection
uv run python deploy.py attach-ncc --profile azure-account-admin      # attach to workspace
uv run python deploy.py setup-secrets --profile <workspace-profile>   # store credentials
```

Then import `aurabc_private_link_test.ipynb` into your Databricks workspace and run it on serverless compute.

### 5. SSH into the proxy VM (optional)

```bash
uv run python deploy.py ssh
# On the VM:
systemctl status haproxy
```

### 6. Clean up

```bash
uv run python deploy.py detach-ncc --profile azure-account-admin  # remove NCC from Databricks
uv run python deploy.py cleanup                                    # delete Azure resource group
uv run python manage_ip_allowlist.py list
uv run python manage_ip_allowlist.py remove --filter-id <ID>
```

## All commands

### Azure infrastructure

| Command | What it does |
|---------|-------------|
| `uv run python deploy.py deploy` | Deploy all Azure infrastructure |
| `uv run python deploy.py allowlist` | Add NAT Gateway IP to Aura BC allowlist |
| `uv run python deploy.py status` | Show resource status, IPs, and power state |
| `uv run python deploy.py outputs` | Show and refresh deployment outputs |
| `uv run python deploy.py ssh` | SSH into the proxy VM via Azure tunnel |
| `uv run python deploy.py cleanup` | Delete all Azure infrastructure |

### Databricks NCC (all accept `--profile <name>`)

| Command | What it does |
|---------|-------------|
| `uv run python deploy.py create-ncc` | Create a Databricks NCC in the NCC_REGION |
| `uv run python deploy.py create-pe-rule` | Add private endpoint rule pointing to the PLS |
| `uv run python deploy.py approve` | Approve pending connections on the PLS |
| `uv run python deploy.py attach-ncc` | Attach the NCC to a Databricks workspace |
| `uv run python deploy.py setup-secrets` | Store Neo4j password in Databricks secret scope `neo4j-aurabc-lb` |
| `uv run python deploy.py detach-ncc` | Detach NCC from workspace, delete rules, delete NCC |

### Validation and IP management

| Command | What it does |
|---------|-------------|
| `uv run python validate_bolt.py` | Test bolt+s:// connectivity from local machine |
| `uv run python manage_ip_allowlist.py list` | List Aura BC IP filters |
| `uv run python manage_ip_allowlist.py add --ip X --description Y` | Add IP to allowlist |
| `uv run python manage_ip_allowlist.py remove --filter-id ID` | Remove an IP filter |

## Test VM validation (py-test/)

Deploys a test VM in eastus with a private endpoint to the PLS, simulating the exact path that Databricks serverless takes. Runs pytest to validate the full chain end-to-end.

```
Test VM (eastus) --> PE --> PLS (eastus) --> ILB --> HAProxy --> NAT GW --> Aura BC
```

### Prerequisites

- LB infrastructure already deployed (steps 1-3 above)
- `deployment-outputs.json` and `.env` populated in the parent directory

### Run

```bash
cd py-test

# 1. Deploy test VM, auto-resolves all params from parent .env + deployment-outputs.json
uv run python deploy_test_vm.py deploy

# 2. Run pytest on the VM via SSH
uv run python deploy_test_vm.py test

# 3. Tear down test VM
uv run python deploy_test_vm.py cleanup
```

The `deploy` command handles everything: creates the resource group in eastus, deploys the Bicep template (VNet, private endpoint to the PLS, VM with cloud-init), waits for cloud-init to install uv and configure `/etc/hosts`, generates a `.env` for the VM with all credentials and IPs, and SCPs all test files to the VM.

### What the tests validate

| Test | What it proves |
|------|---------------|
| `test_hosts_file_entry` | Cloud-init set up `/etc/hosts` (Aura FQDN to PE IP) |
| `test_pe_dns_resolution` | FQDN resolves to the PE private IP |
| `test_pe_tcp_connectivity` | TCP reachable through PE to PLS to LB to HAProxy |
| `test_bolt_through_private_endpoint` | Full bolt+s:// query through the entire private link chain |
| `test_bolt_direct_baseline` | Direct connectivity with VM's own allowlisted IP (bypasses PE) |

### Debugging

Test output is written to `py-test/test-output.log` (gitignored). The console shows a summary; check the log for full tracebacks.

```bash
cat py-test/test-output.log                # full test output
uv run python deploy_test_vm.py ssh        # SSH into the test VM
```

## Key constraints

- **bolt+s:// required.** `neo4j+s://` performs routing table discovery which bypasses the proxy. Validated working against Aura BC on 2026-03-18.
- **Real Aura FQDN required for NCC domain.** Databricks uses the NCC PE rule domain as the TLS SNI hostname. Aura BC rejects connections where the SNI does not match its certificate. Custom private domains do not work.
- **HAProxy health checks must be disabled.** The `check` directive sends plain TCP probes without TLS, which Aura BC drops. This can cause HAProxy to mark the backend as unhealthy.
- **~5 min idle timeout.** Azure Private Link Service enforces approximately 300 seconds. Driver must set `max_connection_lifetime < 300` and `liveness_check_timeout < 300`.
- **NCC region must match workspace region.** The NCC must be in the same region as the workspace.
- **Public internet leg remains.** Traffic from the HAProxy VM to Aura BC traverses the public internet over TLS. Only Aura VDC with native Private Link eliminates this hop.
