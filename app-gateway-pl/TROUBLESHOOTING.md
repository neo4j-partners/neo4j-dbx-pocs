# Application Gateway v2 POC: Troubleshooting Log

Status: **Blocked** — Private Link does not work with L4/TCP Application Gateways.

Date: 2026-03-19

## What We Built

An Azure Application Gateway v2 prototype for connecting Databricks serverless compute to Neo4j Aura Business Critical over Private Link. The goal: replace the current Standard Load Balancer + HAProxy architecture with a managed L4 TCP proxy that preserves TLS SNI end-to-end.

### Deployed Infrastructure

| Resource | Name | Region | Status |
|----------|------|--------|--------|
| Resource Group | `aurabc-appgw-poc-rg` | westus3 | Deployed |
| Public IP | `aurabc-appgw-pip` | westus3 | 172.182.198.60 |
| VNet | `aurabc-appgw-vnet` | westus3 | 10.0.0.0/16 |
| App Gateway v2 | `aurabc-appgw-gw` | westus3 | Running |
| Aura BC Allowlist | filter `f4712b6f-...` | — | Active |

The App Gateway is configured with:
- **L4 TCP listener** on port 7687 (Bolt protocol, TLS passthrough)
- **L7 HTTP listener** on port 80 (added as a workaround attempt, see below)
- **Private Link configuration** (`pl-config`) bound to the frontend IP
- **Backend pool** pointing to `561bfce3.databases.neo4j.io`

### Test Infrastructure (Not Yet Deployed)

A test VM suite in `py-test/` modeled after `aurabc-lb-validation/py-test`:
- Bicep template deploying an Ubuntu VM in eastus with a Private Endpoint to the App Gateway
- pytest suite testing `bolt+s://` and `neo4j+s://` through PE -> App Gateway -> Aura BC
- Self-contained deploy script with PE approval step (App Gateway PEs require manual approval, unlike PLS)

### Project Files

```
app-gateway-pl/
  infra/main.bicep          # App Gateway Bicep (L4 TCP + L7 HTTP + Private Link)
  setup_azure.py            # Consolidated Azure deployment script
  azure-resources.json      # Generated resource manifest (gitignored)
  deploy.py                 # Databricks NCC integration (Phase 3, not yet used)
  validate_bolt.py          # Direct bolt+s:// validation (no PE)
  manage_ip_allowlist.py    # Aura BC IP allowlist management
  py-test/
    infra/main.bicep        # Test VM + Private Endpoint Bicep
    deploy_test_vm.py       # VM deployment orchestrator
    conftest.py             # pytest fixtures with inlined Aura API
    test_appgw_connectivity.py  # 7 connectivity tests
    pyproject.toml
```

## The Blocker: Private Link + L4 TCP Incompatibility

### Error

Creating a Private Endpoint to the Application Gateway fails with:

```
ApplicationGatewayPrivateLinkOperationError:
Cannot perform private link operation on ApplicationGateway
/subscriptions/.../applicationGateways/aurabc-appgw-gw.
Please make sure application gateway has private link configuration.
```

The Private Link configuration exists. It is bound to the frontend IP. The App Gateway is running and healthy. The error is misleading.

### Root Cause

Private Link on Application Gateway validates against **L7 `httpListeners` only**. When Azure's PE creation code checks whether the App Gateway "has private link configuration," it looks for an `httpListener` associated with the frontend IP that has the PL config. L4 `listeners` (TCP protocol) are invisible to this validation.

Evidence:
- The App Gateway has `privateLinkConfigurations[0].provisioningState: Succeeded`
- The frontend IP has `privateLinkConfiguration.id` correctly set
- The PL config has an IP configuration in the PL subnet with `privateLinkServiceNetworkPolicies: Disabled`
- The PE creation fails identically via Bicep and via `az network private-endpoint create`
- Microsoft docs state: "Frontend IP configurations without an associated listener can't be shown as a Target sub-resource" — and the PL validation only inspects `httpListeners`, not L4 `listeners`
- L4 TCP proxy on App Gateway is still in Preview (as of March 2026); Private Link is GA. The features were never integrated.

### What We Tried

**Attempt 1: L4-only App Gateway with Private Link config**

The initial deployment. App Gateway deploys and runs. TCP listener on 7687 works for direct connections. Private Link config is bound to the frontend IP. PE creation fails.

**Attempt 2: Add L7 HTTP listener alongside L4 TCP listener**

Added a minimal HTTP listener on port 80 with a corresponding `httpListener`, `backendHttpSettingsCollection`, and `requestRoutingRule` on the same frontend IP. The theory: the L7 listener satisfies PL validation, and the PL tunnel forwards all ports on the frontend IP (including 7687 for the L4 TCP listener).

Result: Same error. The presence of L4 properties on the gateway may cause Azure to treat it as an L4-capable gateway internally, and PL validation rejects the entire gateway regardless of whether L7 listeners exist. The `sku.family` reports as `Generation_1` when L4 listeners are present, which may be significant.

**Attempt 3 (not executed): Pure L7 App Gateway**

Planned but not deployed. Would have confirmed whether a pure L7 gateway (no L4 properties at all) accepts PE connections. This would isolate whether L4 listeners *existing on the gateway* are the problem, or whether the issue is specific to the frontend IP listener binding. This test would not solve the Bolt protocol problem (Bolt is not HTTP), but would confirm the diagnosis.

## Bicep Property Name Confusion

A secondary issue encountered during development: the L4 TCP properties on Application Gateway use the **same top-level names** as L7 but with different sub-properties.

| Purpose | L7 Property | L4 Property |
|---------|-------------|-------------|
| Listeners | `httpListeners` | `listeners` |
| Routing | `requestRoutingRules` | `routingRules` |
| Backend settings | `backendHttpSettingsCollection` | `backendSettingsCollection` |

Initial external research incorrectly reported that L4 uses `listener` (singular) and `routingRule` (singular). The Bicep compiler rejected these with a clear error. The correct names are all **plural**: `listeners`, `routingRules`. Microsoft docs state: "You can't cross-link Layer 4 and Layer 7 properties."

## Backend Health: Expected "Unhealthy"

The `backendHealth` array reports `561bfce3.databases.neo4j.io` as `Unhealthy`. This is expected and not a problem:
- The L7 HTTP health probe checks port 80 against Aura BC, which only speaks Bolt on 7687
- L4 TCP backend settings use their own health probing mechanism
- Direct `bolt+s://` connections to the App Gateway public IP (172.182.198.60) work, confirming the L4 TCP path is functional

## Next Steps

### Option A: Pure L7 App Gateway (confirm diagnosis)

Deploy a gateway with only L7 `httpListeners` (no L4 `listeners` at all). If PE creation succeeds, this confirms L4 is the blocker. This does not solve the Bolt protocol problem since App Gateway L7 expects HTTP semantics and would reject Bolt frames.

### Option B: Standard Load Balancer + Private Link Service

This is the architecture that `aurabc-lb-validation/` already implements. A Standard Load Balancer with TCP rules fully supports Private Link Service, which in turn supports Private Endpoints. The tradeoff: requires HAProxy or similar for TLS SNI routing, adding operational complexity.

### Option C: Wait for Microsoft to integrate L4 + Private Link

L4 TCP proxy is in Preview. Private Link integration may come when L4 reaches GA. No timeline available. Not viable for near-term production use.

### Option D: File a support request with Microsoft

Ask whether L4 + Private Link is a planned feature or a fundamental architectural limitation. The error message is misleading ("make sure application gateway has private link configuration" when the configuration exists) and suggests this is a validation gap rather than an intentional restriction.

## Key Takeaway

Application Gateway v2 with L4/TCP mode cannot be used with Private Link as of March 2026. The L4 TCP proxy feature and the Private Link feature exist independently on the same SKU but have not been integrated. Any architecture requiring both TCP passthrough and Private Endpoint access should use a Standard Load Balancer with Private Link Service instead.
