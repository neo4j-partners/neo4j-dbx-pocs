# neo4j-dbx-pocs

Proof-of-concept architectures for connecting Azure Databricks serverless compute to Neo4j Aura Business Critical over Azure Private Link.

## Protocol limitation (affects both approaches)

Both approaches require using `bolt+s://` instead of the standard `neo4j+s://` protocol. The `neo4j+s://` scheme triggers routing table discovery, where the driver asks the server for a list of backend hostnames and then tries to connect to them directly. Those hostnames resolve to Aura's public endpoint, which means the driver bypasses the private link path entirely and connections fail.

Using `bolt+s://` forces the driver into direct mode: one connection to one server, no discovery, no routing. This works, but it means you lose client-side routing, automatic failover, and read/write splitting that `neo4j+s://` provides. For applications that rely on these features, this is a meaningful functional limitation. Both approaches share this constraint and there is no workaround short of upgrading to Aura VDC with native Private Link.

## The two approaches

### 1. Load Balancer + HAProxy (`aurabc-lb-validation/`)

**Status: validated end-to-end**

Deploys an Azure Internal Load Balancer fronted by a Private Link Service, with an HAProxy VM that forwards Bolt traffic to Aura BC over the public internet through a NAT Gateway.

```
Databricks Serverless
    |  NCC Private Endpoint
    v
Private Link Service --> Internal Load Balancer --> HAProxy VM --> NAT Gateway --> Aura BC
```

The HAProxy VM is necessary because an Internal Load Balancer can only route to private IPs in its own VNet. Aura BC lives outside your Azure network, so you need a local proxy that the load balancer can target, which then forwards traffic out to Aura BC.

**Pros:**
- Proven and fully validated
- True Layer 4 pass-through on the load balancer (TLS SNI always preserved, no configuration risk)
- Session affinity via source IP pinning
- Lower base cost for the load balancer itself (~$18/month)

**Cons:**
- Requires a VM you must patch, monitor, and keep running
- NAT Gateway required for a static outbound IP that Aura BC can allowlist
- More infrastructure to manage (VNet, LB, PLS, VM, NAT Gateway)
- Total cost with VMs and NAT Gateway is comparable to the App Gateway approach

### 2. Application Gateway v2 (`app-gateway-pl/`)

**Status: phased deployment validated, end-to-end Bolt testing pending**

Uses Azure Application Gateway v2 as an L4 TCP proxy with Private Link. No VMs, no HAProxy, no NAT Gateway, no Load Balancer. The gateway handles TCP passthrough on port 7687 and preserves TLS SNI.

```
Databricks Serverless
    |  NCC Private Endpoint
    v
Application Gateway v2 (L4 TCP listener, port 7687) --> Aura BC
```

Requires a phased deployment because App Gateway's Private Link validation only recognizes L7 HTTP listeners. You deploy with an HTTP listener first, create the Private Endpoint, then add the L4 TCP listener in a second phase. Azure does not re-validate after the tunnel is established.

**Pros:**
- Fully managed Azure service (no VMs to maintain)
- Fewer moving parts (single resource instead of LB + VM + NAT Gateway)
- Application Gateway resolves FQDNs natively and connects directly to Aura BC
- Zone-redundant by default

**Cons:**
- Phased deployment is a workaround for a platform limitation (L4 + Private Link not integrated)
- Higher base cost (~$175+/month for the gateway)
- No session affinity at Layer 4
- End-to-end Bolt connectivity not yet validated through the full private link chain

## Shared constraints

Both approaches share the same fundamental limitations imposed by Azure Private Link and the Neo4j protocol:

- **bolt+s:// required:** The `neo4j+s://` scheme performs routing table discovery that bypasses the private link path. You must use `bolt+s://`, which means no client-side routing, no automatic failover, and no read/write splitting.
- **Real Aura FQDN required for NCC domain:** Databricks uses the NCC private endpoint rule domain as the TLS SNI hostname. Aura BC rejects connections where the SNI does not match its certificate. You cannot use a custom private domain.
- **~5 minute idle timeout:** Azure Private Link enforces an idle timeout of roughly 300 seconds. The Neo4j driver must set `max_connection_lifetime` and `liveness_check_timeout` below 300 seconds to prevent silent connection drops.
- **NCC region must match workspace region:** The Databricks Network Connectivity Configuration must be created in the same Azure region as the workspace. The private endpoint can point cross-region to the infrastructure.
- **Public internet leg remains:** Traffic from the proxy (LB approach) or gateway (App Gateway approach) to Aura BC still traverses the public internet over TLS. Only Aura VDC with native Azure Private Link eliminates this hop entirely.

## Background

For detailed research on the networking constraints, protocol analysis, and decision rationale behind these approaches, see the internal documentation at `/Users/ryanknight/projects/cloud-integration/databricks`. Key files include `LOAD_BALANCER_VS_APP_GATEWAY.md` (deep comparison), `NCC_AURA_BC.md` (why NSP and direct approaches do not work), and `WHY.md` (protocol behavior analysis).
