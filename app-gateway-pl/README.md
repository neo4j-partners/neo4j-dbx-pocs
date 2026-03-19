# Application Gateway POC — Databricks Serverless to Neo4j Aura BC

Prototype using Azure Application Gateway v2 as a Layer 4 TCP proxy between Databricks serverless compute and Neo4j Aura Business Critical. The gateway preserves TLS SNI end-to-end in TCP passthrough mode, while Private Link provides the Databricks-to-gateway connectivity.

This is an alternative prototype alongside the [load balancer POC](../aurabc-lb-validation/). See [WAPG.md](../WAPG.md) for the full proposal.

## Prerequisites

- Azure CLI (`az`) logged in with a subscription
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- Databricks CLI (for NCC and secrets commands)
- A Neo4j Aura Business Critical instance
- Aura Admin API credentials (client ID + secret)

## Quick Start

```bash
# 1. Set up environment
cp .env.sample .env
# Edit .env with your Neo4j, Aura API, Azure, and Databricks values

# 2. Deploy Azure infrastructure and add IP to Aura BC allowlist (single command)
#    Creates: resource group, VNet, public IP, Application Gateway, Private Link config
#    Writes:  azure-resources.json with full resource manifest
uv run python setup_azure.py

# 3. Check status — verify App Gateway is healthy and backend is reachable
uv run python deploy.py status

# 4. Create Databricks NCC and private endpoint rule
uv run python deploy.py create-ncc --profile <databricks-cli-profile>
uv run python deploy.py create-pe-rule --profile <databricks-cli-profile>

# 5. Approve the pending private endpoint connection
uv run python deploy.py approve

# 6. Attach NCC to workspace (wait ~10 min for propagation)
uv run python deploy.py attach-ncc --profile <databricks-cli-profile>

# 7. Store Neo4j credentials in Databricks secrets
uv run python deploy.py setup-secrets --profile <databricks-cli-profile>

# 8. Test connectivity from a Databricks serverless notebook
#    (see Phase 4 in WAPG_PLAN.md for notebook code)
```

## Validate Locally

Before setting up the Databricks Private Link path, validate direct connectivity to Aura BC:

```bash
uv run python validate_bolt.py
```

This tests `neo4j+s://`, `bolt+s://`, and both schemes with Private Link keepalive settings.

## Resource Manifest

After running `setup_azure.py`, all deployed resources are recorded in `azure-resources.json`:

```
azure-resources.json
├── metadata          # deployment timestamp, subscription, prefix, Aura FQDN
├── resourceGroup     # name, location, resource ID
├── publicIp          # name, IP address, SKU, allocation method
├── vnet              # name, address space, subnets with delegations
├── applicationGateway
│   ├── name, resourceId, provisioningState, operationalState
│   ├── listener      # TCP listener on port 7687
│   ├── backendPool   # Aura BC FQDN
│   ├── backendSetting # TCP backend timeout
│   ├── privateLinkConfig  # PL config name and ID
│   └── privateEndpointConnections
├── backendHealth     # per-server health status
└── allowlist         # Aura BC IP filter ID and IP address
```

The Databricks NCC commands in `deploy.py` read from this file to get the Application Gateway resource ID and Private Link config name.

## Cleanup

```bash
# Remove Azure resources
uv run python deploy.py cleanup

# Remove the App Gateway IP from Aura BC allowlist
uv run python manage_ip_allowlist.py list
uv run python manage_ip_allowlist.py remove --filter-id <ID>

# Detach and delete NCC
uv run python deploy.py detach-ncc --profile <databricks-cli-profile>
```

## Architecture

```
Databricks Serverless
    |
    | NCC Private Endpoint
    v
Azure Application Gateway v2 (TCP listener, port 7687)
    |
    | Outbound TCP (public IP allowlisted)
    v
Neo4j Aura Business Critical
```

Key differences from the load balancer POC:
- No proxy VMs, NAT Gateway, or HAProxy configuration
- Application Gateway originates its own outbound connections
- Two subnets instead of three (App Gateway + Private Link)
- TCP passthrough mode preserves TLS SNI end-to-end

## Scripts

| Script | Purpose |
|--------|---------|
| `setup_azure.py` | Deploy all Azure infrastructure (single command) |
| `deploy.py` | Databricks NCC integration and post-deployment operations |
| `validate_bolt.py` | Test bolt+s:// and neo4j+s:// connectivity |
| `manage_ip_allowlist.py` | Manage Aura BC IP allowlist entries |

### deploy.py commands

| Command | Description |
|---------|-------------|
| `status` | Show App Gateway health, backend status, Private Link connections |
| `cleanup` | Delete the resource group and all resources |
| `create-ncc` | Create a Databricks NCC |
| `create-pe-rule` | Create private endpoint rule pointing to App Gateway |
| `approve` | Approve pending private endpoint connections |
| `attach-ncc` | Attach NCC to a Databricks workspace |
| `setup-secrets` | Store Neo4j credentials in Databricks secrets |
| `detach-ncc` | Detach and delete NCC from Databricks |
