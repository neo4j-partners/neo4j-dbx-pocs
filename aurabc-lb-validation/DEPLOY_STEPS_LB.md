# Aura BC Load Balancer — Deployment & Test Checklist

**Date:** 2026-03-19
**Azure Infra Region:** eastus
**NCC Region:** eastus (must match workspace)
**Workspace:** partner-demo-workspace-v2 (1098933906466604)
**Aura BC Instance:** f5919d06 (staples-bc-validation)
**NCC ID:** 2a40363d-df07-4466-95ef-096ac43def63

## Prerequisites

- [x] Azure CLI logged in (`az login`)
- [x] Databricks CLI profile configured and verified (`databricks auth token --profile azure-account-admin`)
- [x] `.env` populated with Neo4j and Databricks credentials
- [x] SSH key available

## Azure Infrastructure

- [x] `deploy` — Azure infra deployed (VNet, LB, HAProxy VM, NAT GW, PLS)
- [x] `allowlist` — NAT Gateway IP (20.106.75.1) added to Aura BC allowlist
- [x] `status` — All resources healthy (VM running, LB Succeeded, PLS Succeeded)

## Databricks NCC Setup

- [x] `create-ncc` — NCC created in **eastus**
- [x] `create-pe-rule` — PE rule created, status ESTABLISHED (auto-approved, no pending state)
- [x] `approve` — 2 connections already Approved, no pending
- [x] `attach-ncc` — NCC attached to workspace partner-demo-workspace-v2
- [x] `setup-secrets` — Password stored in scope `neo4j-aurabc-lb`

## Validation

- [x] Import `aurabc_private_link_test.ipynb` into Databricks workspace
- [x] Run notebook on serverless compute
- [x] TCP connectivity test passes (77ms)
- [x] Neo4j driver test passes — `bolt+s://f5919d06.databases.neo4j.io:7687`, connected in 353ms, query succeeded

## Teardown

- [ ] `detach-ncc` — Swap to placeholder NCC, delete rules, delete NCC
- [ ] `cleanup` — Delete Azure resource group
- [ ] Remove NAT IP from Aura BC allowlist

---

## Execution Log

### Status Check
> **PASS** — 2026-03-19
> All resources healthy. VM running at 10.0.2.4, NAT IP 20.106.75.1, LB and PLS both Succeeded.

### Create NCC
> **PASS** — 2026-03-19
> First attempt created NCC in westus3, but `attach-ncc` failed: "NCC must be in the same region as the workspace (eastus)".
> Deleted the westus3 NCC and recreated in eastus. Cross-region PE rule works fine.
> NCC ID: `2a40363d-df07-4466-95ef-096ac43def63`
>
> **Finding:** The `create-ncc` command uses `NCC_REGION` (falling back to `AZURE_LOCATION`) as the NCC region. When the workspace is in a different region than the Azure infra, set `NCC_REGION` in `.env`.

### Create PE Rule
> **PASS (2nd attempt)** — 2026-03-19
>
> First attempt used domain `neo4j-aurabc.private.neo4j.com` (arbitrary private domain). TCP connected but TLS failed with `SSLEOFError` — Aura BC rejected the connection because the SNI hostname didn't match its certificate.
>
> **Finding:** The NCC PE rule domain becomes the SNI hostname in the TLS ClientHello. Aura BC enforces strict SNI matching. The domain **must** be the real Aura FQDN (`f5919d06.databases.neo4j.io`), not a made-up private domain.
>
> Deleted old rule `b2f9f7ad`, created new rule `f485c27c` with domain `f5919d06.databases.neo4j.io`. Status ESTABLISHED, auto-approved.

### Approve Connection
> **PASS** — 2026-03-19
> All connections auto-approved.

### Attach NCC
> **PASS** — 2026-03-19
> NCC attached to workspace `partner-demo-workspace-v2`.

### Setup Secrets
> **PASS** — 2026-03-19
> Scope `neo4j-aurabc-lb` created. Password stored successfully.
> Used workspace profile `azure-rk-knight` (not the account admin profile).

### Notebook Test
> **PASS** — 2026-03-19
>
> After fixing the SNI mismatch (see TROUBLESHOOTING.md), the notebook connected successfully:
>
> ```
> URI: bolt+s://f5919d06.databases.neo4j.io:7687
> [PASS] Connected and authenticated in 352.8ms
> [PASS] Query result: Connected over Private Link via bolt+s
> Server: Neo4j Kernel ['5.27-aura'], Cypher ['5', '25']
> Total: 761.3ms
> ```
>
> Three issues were found and fixed during testing:
> 1. NCC region must match workspace (eastus) → added `NCC_REGION` env var
> 2. HAProxy `check` directive caused non-TLS probes that interfered with Aura BC → removed `check`
> 3. NCC PE rule domain must be real Aura FQDN for correct TLS SNI → changed from private domain to `f5919d06.databases.neo4j.io`
