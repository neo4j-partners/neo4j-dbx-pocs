# Making Application Gateway Work: Databricks Serverless to Neo4j Aura BC

**Date:** 2026-03-20
**Status:** Complete — All 3 phases passed
**Constraint:** This POC must use Azure Application Gateway v2. No alternative architectures (LB+HAProxy, PLS Direct Connect, etc.) are in scope.

---

## Execution Log

| Phase | Status | Timestamp | Notes |
|-------|--------|-----------|-------|
| Phase 1: Pure L7 Gateway + PL + PE | PASS | 2026-03-20 | PE created and auto-approved. Pure L7 gateway + PL works. PE IP: 10.1.1.4, Public IP: 20.169.136.165 |
| Phase 2: Add L4 TCP Listeners | PASS | 2026-03-20 | L4 TCP listener added, PE connection still Approved. Gateway: Succeeded/Running. |
| Phase 3: End-to-End Bolt Test | PASS | 2026-03-20 | bolt+s:// and neo4j+s:// both work through PE → App GW L4 → Aura BC. VM IP: 20.121.211.36, PE IP: 10.1.2.4. 6/6 tests passed. |

---

## Context

| Parameter | Value |
|-----------|-------|
| Stage | POC |
| Workspace count | Single |
| Region | eastus (all infrastructure co-located) |
| Budget | Not a constraint |
| Goal | Simplest possible working POC with App Gateway |

---

## The Problem

Application Gateway v2 can do two things independently:

1. **L4 TCP proxy** on port 7687 with TLS passthrough (preserves Bolt SNI end-to-end)
2. **Private Link** on the frontend IP (accepts Databricks NCC private endpoints)

It cannot do both at the same time. Private Link validation only checks `httpListeners`. L4 `listeners` are invisible to the validation code. PE creation fails with a misleading error even though the Private Link configuration exists and is correctly bound.

The first POC attempt tried three variations and hit the same wall each time:

| Attempt | Result | Why |
|---------|--------|-----|
| L4-only gateway | PE creation fails | PL validation finds no `httpListeners` on the frontend IP |
| L4 + L7 hybrid (both listeners from initial deploy) | PE creation fails | L4 properties on the gateway appear to change its internal classification; PL validation rejects the entire gateway |
| Pure L7 gateway | **Not tested** | Planned but never executed |

The pure L7 test is the critical gap. It was skipped because "Bolt is not HTTP, so even if PE creation succeeds, no Bolt traffic could flow through an L7 listener." That reasoning is correct for a pure L7 architecture but misses a key insight about how Private Link tunnels work.

---

## The Hypothesis: L7-First Deployment, Then Add L4

Private Link on Application Gateway operates at the **network layer**, not the listener layer. The PL tunnel forwards all TCP traffic to the frontend IP. The validation that checks for `httpListeners` only runs at PE creation time, not continuously. Once the tunnel is established, traffic to any port on that frontend IP should flow through, regardless of which listener type handles it.

This leads to a phased deployment strategy:

1. Deploy a pure L7 App Gateway (zero L4 properties)
2. Configure Private Link on the frontend IP
3. Create a Private Endpoint to the gateway (should succeed: L7 httpListener satisfies PL validation)
4. Get the PE approved and established
5. Update the App Gateway to add L4 TCP listeners on port 7687 to the same frontend IP
6. Test whether Bolt traffic flows through the PE tunnel to the L4 listener

The question this answers: does Azure re-validate Private Link configuration when L4 listeners are added to an existing gateway, or does the established PL tunnel continue to forward traffic?

---

## Experiment Plan

Three phases. Each phase has a clear pass/fail gate before proceeding.

### Phase 1: Pure L7 App Gateway + Private Link + PE Creation

**Goal:** Confirm that a pure L7 gateway (no L4 properties whatsoever) accepts Private Endpoint connections.

**Deploy:**
- App Gateway v2 in eastus with a single `httpListener` on port 80 (or 443 with a self-signed cert)
- Backend pool pointing to the Aura BC FQDN (backend health will be "unhealthy" since Aura doesn't speak HTTP; this is expected and irrelevant)
- Private Link configuration on the frontend IP
- Private Link subnet with `privateLinkServiceNetworkPolicies: Disabled`

**Test:**
- Create a Private Endpoint to the App Gateway using the frontend IP configuration name as the `group-id`
- If PE creation succeeds, approve it

**Pass criteria:** PE status transitions from Pending to Approved. The Private Link tunnel is established.

**Fail criteria:** PE creation fails with the same `ApplicationGatewayPrivateLinkOperationError`. If this happens, the App Gateway Private Link feature itself has additional constraints beyond what the docs describe, and we need to investigate further (potentially a support ticket to Microsoft).

**Bicep approach:** New Bicep template (`infra/main-phase1.bicep`) with only L7 properties. No `listeners`, `routingRules`, or `backendSettingsCollection` (the L4 property names). Only `httpListeners`, `requestRoutingRules`, and `backendHttpSettingsCollection`.

**References:**
- [Application Gateway Private Link configuration](https://learn.microsoft.com/azure/application-gateway/private-link-configure)
- [Microsoft Q&A: PE creation steps](https://learn.microsoft.com/en-us/answers/questions/2099854/azure-cli-cannot-create-private-endpoint-for-appli) (confirms `group-id` must be the frontend IP config name)

### Phase 2: Add L4 TCP Listener to the Existing Gateway

**Goal:** Determine whether adding L4 listeners to a gateway with an active Private Link tunnel breaks the tunnel or prevents the update.

**Prerequisites:** Phase 1 passed. PE is Approved and the Private Link tunnel is established.

**Update the App Gateway:**
- Add an L4 `listeners` entry: TCP protocol on port 7687, bound to the same frontend IP
- Add a `backendSettingsCollection` entry: TCP protocol, port 7687, timeout 300s
- Add a `routingRules` entry: connecting the L4 listener to the backend pool
- Keep the existing L7 `httpListener` and its routing rule in place (removing it might break the PL config)

**Three possible outcomes:**

| Outcome | What it means | Next step |
|---------|---------------|-----------|
| Update succeeds, PL tunnel remains active | The hypothesis is correct. L4 traffic can flow through the PL tunnel. | Proceed to Phase 3 |
| Update succeeds, PL tunnel breaks (PE disconnected) | Azure re-validates PL config on update and rejects the new L4 properties | Investigate whether re-creating the PE after the update works |
| Update fails (Bicep/ARM rejects the deployment) | Azure prevents L4 properties from being added to a PL-enabled gateway | Dead end for this approach |

**Test after update:**
- Check PE connection status (still Approved?)
- Check App Gateway operational state (still Running?)
- Check that the L4 listener appears in the gateway configuration

**References:**
- [Application Gateway TCP/TLS proxy overview](https://learn.microsoft.com/azure/application-gateway/tcp-tls-proxy-overview) (L4 listener properties)
- [Application Gateway FAQ](https://learn.microsoft.com/en-us/azure/application-gateway/application-gateway-faq) ("Both Layer 7 and Layer 4 routing through application gateway use the same frontend IP configuration")

### Phase 3: End-to-End Bolt Connectivity Through the PE

**Goal:** Validate that Bolt traffic from a test VM traverses the Private Link tunnel and reaches Aura BC through the App Gateway's L4 TCP proxy.

**Prerequisites:** Phase 2 passed. L4 listener is active and PE tunnel is intact.

**Deploy test infrastructure** (adapted from `py-test/`):
- Test VM in eastus with a Private Endpoint to the App Gateway
- `/etc/hosts` entry mapping the Aura BC FQDN to the PE private IP
- The App Gateway public IP allowlisted on Aura BC

**Run the test suite:**

| Test | What it proves |
|------|---------------|
| PE DNS resolution | FQDN resolves to PE private IP via `/etc/hosts` |
| TCP connectivity to PE | Port 7687 reachable through PE to App Gateway |
| `bolt+s://` through PE | Full Bolt query through PE → App GW L4 listener → Aura BC |
| `neo4j+s://` through PE | Routing protocol test (expected to fail; validates `bolt+s://` requirement) |

**Pass criteria:** `bolt+s://` query returns results through the PE. This proves the full chain works:

```
Test VM → PE → App Gateway (L4 TCP listener, port 7687) → Aura BC
```

**If Phase 3 passes**, the architecture for the Databricks integration becomes:

```
Databricks Serverless (eastus)
    |
    |  NCC private endpoint
    v
Application Gateway v2 (eastus)
    |  L4 TCP listener, port 7687
    |  TLS passthrough (preserves Bolt SNI)
    v
Neo4j Aura BC (*.databases.neo4j.io:7687)
```

No VMs. No HAProxy. No NAT Gateway. No Load Balancer. The App Gateway handles outbound connectivity to Aura BC on its own (via its public IP, which gets allowlisted).

---

## If the Experiment Fails

If Phase 1 or Phase 2 fails, there are two remaining App Gateway-only avenues before exhausting options:

### Fallback A: Create PE Before Any Listeners, Then Add Both L7 and L4

If Phase 1 shows that a pure L7 gateway works but Phase 2 shows that adding L4 afterward breaks things, try the reverse order: deploy the gateway with only the Private Link configuration and no listeners at all, create the PE, then add both L7 and L4 listeners simultaneously. This tests whether the PL validation runs at PE creation time or at gateway update time.

### Fallback B: Microsoft Support Ticket

If the experiments confirm that L4 + Private Link is fundamentally incompatible today, file a support request with two specific asks:

1. **Is L4 + Private Link integration planned?** The L4 TCP proxy is in Preview. Private Link is GA. The validation gap may be an oversight that gets fixed at L4 GA.
2. **Is there a private preview or feature flag** that enables L4 listener validation for Private Link? Preview features sometimes have undocumented flags.

Include the error message, the Bicep template, and the evidence that the PL configuration exists and is correctly bound. The misleading error message (`"Please make sure application gateway has private link configuration"` when the configuration exists) suggests this is a validation gap rather than an intentional restriction.

---

## Implementation: What to Build

### New Bicep Templates

| File | Purpose |
|------|---------|
| `infra/main-phase1.bicep` | Pure L7 App Gateway + Private Link (no L4 properties) |
| `infra/main-phase2.bicep` | Same gateway with L4 TCP listeners added |
| `py-test/infra/main.bicep` | Test VM + PE (already exists, needs region update to eastus) |

### Updated Scripts

| Script | Change |
|--------|--------|
| `setup_azure.py` | Support phased deployment (Phase 1 template, then Phase 2 update) |
| `py-test/deploy_test_vm.py` | Update default region to eastus |
| `validate_bolt.py` | No change needed |

### Key Constraints to Carry Forward

These were discovered in the `aurabc-lb-validation/` project and apply regardless of the proxy architecture:

| Constraint | Detail |
|------------|--------|
| `bolt+s://` required | `neo4j+s://` triggers routing discovery that bypasses the PE |
| Real Aura FQDN as NCC domain | Databricks uses PE rule domain as TLS SNI; Aura BC rejects unrecognized SNI |
| ~300s idle timeout | App Gateway Private Link has the same ~5 min idle timeout as PLS |
| NCC region matches workspace | NCC in eastus for eastus workspace; App Gateway also in eastus |

---

## Reference Links

| Topic | URL |
|-------|-----|
| App Gateway TCP/TLS proxy overview | https://learn.microsoft.com/azure/application-gateway/tcp-tls-proxy-overview |
| App Gateway Private Link | https://learn.microsoft.com/azure/application-gateway/private-link |
| App Gateway Private Link config | https://learn.microsoft.com/azure/application-gateway/private-link-configure |
| App Gateway FAQ (L4 + L7 same frontend IP) | https://learn.microsoft.com/en-us/azure/application-gateway/application-gateway-faq |
| MS Q&A: PE creation steps and group-id | https://learn.microsoft.com/en-us/answers/questions/2099854/azure-cli-cannot-create-private-endpoint-for-appli |
| Databricks: Private connectivity to VNet resources | https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/pl-to-internal-network |
| Databricks: Manage PE rules (supported resources) | https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/manage-private-endpoint-rules |
| Databricks: Serverless private link | https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/serverless-private-link |
| Azure Private Link Service overview | https://learn.microsoft.com/azure/private-link/private-link-service-overview |
