# Application Gateway POC — Deployment & Test Checklist

**Date:** 2026-03-XX
**Azure Infra Region:** eastus
**NCC Region:** eastus (must match workspace)
**Workspace:** <workspace-name> (<workspace-id>)
**Aura BC Instance:** <instance-id> (<instance-name>)
**NCC ID:** <ncc-id>

## Prerequisites

- [ ] Azure CLI logged in (`az login`)
- [ ] Databricks CLI profile configured and verified (`databricks auth token --profile <profile>`)
- [ ] `.env` populated with Neo4j, Aura API, Azure, and Databricks credentials
- [ ] `NEO4J_DOMAIN` set to real Aura FQDN (e.g. `xxxxxxxx.databases.neo4j.io`)
- [ ] `NCC_REGION` set to workspace region (e.g. `eastus`)

## Azure Infrastructure

- [ ] `setup_azure.py phase1` — Pure L7 gateway + Private Link + Private Endpoint deployed (~8 min)
- [ ] `setup_azure.py phase2` — L4 TCP listener added on port 7687 (~8 min)
- [ ] `setup_azure.py status` — App Gateway running, PE approved, L4 listener active
- [ ] `manage_ip_allowlist.py add` — App Gateway public IP added to Aura BC allowlist

## Databricks NCC Setup

- [ ] `deploy.py create-ncc --profile <profile>` — NCC created in workspace region
- [ ] `deploy.py create-pe-rule --profile <profile>` — PE rule created with real Aura FQDN as domain
- [ ] `deploy.py approve` — Private endpoint connections approved
- [ ] `deploy.py attach-ncc --profile <profile>` — NCC attached to workspace
- [ ] `deploy.py setup-secrets --profile <profile>` — Password stored in scope `neo4j-appgw-poc`

## Validation

- [ ] Import `appgw_private_link_test.ipynb` into Databricks workspace
- [ ] Update `NEO4J_DOMAIN` in notebook config cell to match your Aura FQDN
- [ ] Run notebook on serverless compute
- [ ] TCP connectivity test passes
- [ ] Neo4j driver test passes — `bolt+s://<fqdn>:7687`, connected and query succeeded

## Teardown

- [ ] `deploy.py detach-ncc --profile <profile>` — Swap to placeholder NCC, delete rules, delete NCC
- [ ] `setup_azure.py cleanup` — Delete Azure resource group
- [ ] `manage_ip_allowlist.py remove --filter-id <ID>` — Remove App Gateway IP from Aura BC allowlist

---

## Execution Log

### Phase 1: Azure Infrastructure
> **Status:** pending
>

### Phase 2: L4 TCP Listener
> **Status:** pending
>

### Allowlist
> **Status:** pending
>

### Create NCC
> **Status:** pending
>

### Create PE Rule
> **Status:** pending
>

### Approve Connection
> **Status:** pending
>

### Attach NCC
> **Status:** pending
>

### Setup Secrets
> **Status:** pending
>

### Notebook Test
> **Status:** pending
>
