# Architecture: Application Gateway Private Link to Neo4j Aura BC

Databricks serverless compute and Neo4j Aura Business Critical each run in provider-managed Azure subscriptions outside the customer's network. Connecting them privately requires an intermediary in the customer's subscription that can accept Private Link connections from Databricks and forward traffic to Aura BC, all while preserving the TLS Server Name Indication (SNI) hostname that Aura uses to route connections to the correct database instance.

Azure Application Gateway v2 fills this role. This document explains the architecture, how the NCC multi-domain private endpoint rule solved routing table hostname resolution, and why L4 TCP passthrough is critical for SNI preservation.

## How Aura BC Routes Connections

Understanding Aura BC's connection handling explains why SNI preservation dominates every design decision in this architecture.

When a client connects to an Aura BC instance, the FQDN (e.g., `f5919d06.databases.neo4j.io`) resolves to a shared ingress endpoint that serves many customers and many database instances behind a single IP address. Aura reads the SNI value from the TLS ClientHello to determine which database instance the connection belongs to. The SNI is the only signal Aura has before the TLS handshake completes. Without a recognized SNI, the connection is rejected.

This constraint is absolute. Any intermediary in the network path that terminates TLS and re-establishes it must send the correct SNI on the new connection. If it sends its own hostname, a load balancer hostname, or no hostname at all, Aura cannot route the connection and drops it. The safest approach is to never terminate TLS at all, which is exactly what L4 TCP passthrough achieves.

Aura BC uses a wildcard TLS certificate (`*.production-orch-*.neo4j.io`) that covers all cluster member hostnames returned in routing table discovery. This was verified via `openssl s_client` with each routing table hostname as the SNI, confirming `Verify return code: 0 (ok)` for all three members.

## The Routing Table Discovery Challenge

The `neo4j+s://` protocol adds a second layer of complexity beyond SNI preservation.

After establishing the initial connection, the Neo4j driver sends a ROUTE message over the Bolt protocol on port 7687. The server responds with a routing table containing cluster member hostnames for routers, readers, and writers:

```
Routers:
  p-xxxxxxxx-xxxx-0003.production-orch-1275.neo4j.io:7687
  p-xxxxxxxx-xxxx-0002.production-orch-1275.neo4j.io:7687
  p-xxxxxxxx-xxxx-0001.production-orch-1275.neo4j.io:7687

Readers:
  p-xxxxxxxx-xxxx-0002.production-orch-1275.neo4j.io:7687
  p-xxxxxxxx-xxxx-0003.production-orch-1275.neo4j.io:7687

Writers:
  p-xxxxxxxx-xxxx-0001.production-orch-1275.neo4j.io:7687
```

Three facts about these hostnames matter.

First, they are in a completely different domain from the connection FQDN. The client connects to `*.databases.neo4j.io`, but the routing table returns `*.production-orch-*.neo4j.io`. These are not subdomains of each other.

Second, all routing table hostnames resolve to the same IP address as the connection FQDN. Aura routes all cluster member traffic through the same edge endpoint and uses TLS SNI to distinguish between members. The different hostnames are routing labels, not different servers at different addresses.

Third, the routing table is fetched over Bolt on port 7687, not the HTTP API on port 7473. Earlier analysis incorrectly assumed routing table discovery required the HTTP API. This led to several dead-end approaches involving port splitting before the actual mechanism was identified.

### Why This Breaks Without Intervention

When Databricks serverless uses `neo4j+s://`, the following sequence occurs:

1. The driver resolves `f5919d06.databases.neo4j.io`. The NCC private endpoint rule matches this domain and routes the DNS lookup through the private endpoint. The connection flows through the tunnel to the Application Gateway and out to Aura BC. This succeeds.

2. Over this connection, the driver sends a ROUTE message. Aura responds with the routing table containing three `p-*.production-orch-*.neo4j.io` hostnames.

3. The driver attempts to connect to one of these routing hostnames. Databricks resolves the hostname. The NCC private endpoint rule only matches `f5919d06.databases.neo4j.io`. The routing hostname does not match. NCC does not intercept the DNS lookup.

4. The hostname resolves to its public IP. The driver connects directly to Aura BC from a Databricks serverless IP address that is not in Aura's IP allowlist. Aura rejects the connection.

The initial connection through the tunnel succeeds, but every subsequent connection to a routing table member fails. This is why early testing required `bolt+s://`, which skips the ROUTE message entirely and sends all queries over the original connection. It works, but it sacrifices client-side routing, automatic failover, and read/write splitting.

## Architecture

```
Databricks Serverless
    |
    |  NCC Private Endpoint (10.1.1.x)
    |  domain_names: [connection FQDN, 3 routing table hostnames]
    v
Private Link tunnel (Azure backbone, never touches public internet)
    |
    v
Application Gateway v2
    Frontend IP: public (20.x.x.x) with Private Link config
    L4 TCP listener: port 7687, TLS passthrough
    Backend pool: Aura BC FQDN
    |
    |  Outbound TCP via gateway public IP
    |  (IP allowlisted on Aura BC)
    v
Neo4j Aura Business Critical (*.databases.neo4j.io:7687)
```

The architecture uses two subnets within a single VNet:

- **Application Gateway subnet** hosts the gateway with the required subnet delegation. The gateway gets a public IP for outbound connectivity to Aura BC and a Private Link configuration on its frontend for inbound PE connections.
- **Private Link subnet** hosts the private endpoint that receives the NCC connection from Databricks.

### Component Roles

**Application Gateway v2** serves as the L4 TCP proxy. It accepts inbound connections from the Private Link tunnel on port 7687 and opens corresponding outbound connections to the Aura BC FQDN. In L4 mode, the gateway copies bytes bidirectionally without inspecting the payload, preserving TLS SNI and all protocol semantics. It also provides the outbound public IP that is allowlisted on Aura BC, eliminating the need for a separate NAT Gateway.

**NCC Private Endpoint Rule** controls DNS interception in Databricks serverless compute. When the Neo4j driver resolves any hostname listed in the PE rule's `domain_names` array, NCC intercepts the lookup and routes the resulting TCP connection through the private endpoint to the Application Gateway. The PE rule contains all four domains: the connection FQDN and the three routing table member hostnames.

**Private Link** provides a private, backbone-only connection between the Databricks-managed private endpoint and the Application Gateway. Traffic between these two points never touches the public internet.

### Key Properties

- **No VMs.** The Application Gateway is a fully managed Azure service. No operating system to patch, no HAProxy to configure, no process to monitor.
- **No NAT Gateway.** The gateway originates outbound connections from its own public IP. That IP is added to Aura BC's IP allowlist directly.
- **Zone-redundant by default.** Application Gateway v2 distributes across availability zones automatically.
- **Single resource.** One Azure resource replaces the combination of Internal Load Balancer, HAProxy VM, NAT Gateway, and Private Link Service required by the load balancer approach.

## TLS SNI Passthrough

The Application Gateway's L4 TCP listener operates in pure passthrough mode. When a TCP connection arrives on port 7687, the gateway opens a corresponding connection to the backend (Aura BC FQDN) and copies bytes in both directions. It does not parse the TLS handshake, inspect the SNI, validate certificates, or modify any payload bytes.

The TLS session is established end-to-end between the Neo4j driver in Databricks and the Aura BC ingress endpoint. The driver sends the target hostname as the SNI in its ClientHello. The gateway forwards that ClientHello byte-for-byte. Aura reads the SNI, routes to the correct database instance, and completes the handshake. From TLS's perspective, the gateway does not exist.

This property is critical because NCC uses each PE rule domain as the SNI hostname when initiating connections through the private endpoint. If the gateway terminated TLS, it would need to re-establish a new TLS session to Aura with the correct SNI, introducing certificate management complexity and a potential point of failure. L4 passthrough eliminates this entirely.

## The NCC Multi-Domain Solution

The solution to routing table hostname resolution is a single NCC private endpoint rule with multiple domains.

A Databricks NCC PE rule accepts up to 10 entries in its `domain_names` array. NCC intercepts DNS lookups for every listed domain and routes the resulting connections through the private endpoint. By listing both the connection FQDN and all three routing table member hostnames (four domains total), every connection the Neo4j driver makes is routed through the Private Link tunnel, whether that connection is to the initial server or to a routing table member.

The update is a single PATCH API call:

```
PATCH /api/2.0/accounts/{ACCOUNT_ID}/network-connectivity-configs/{NCC_ID}/private-endpoint-rules/{RULE_ID}?update_mask=domain_names
```

The NCC does not need to be detached from the workspace. Changes propagate in approximately five minutes. Running serverless compute should be restarted afterward to pick up the new DNS routing.

The `deploy.py update-pe-domains` command automates this: it connects to Aura BC, fetches the routing table, extracts the member hostnames, and PATCHes the PE rule with all four domains.

### Why This Works End-to-End

The multi-domain PE rule solves two problems simultaneously:

1. **DNS resolution.** NCC intercepts DNS for all four domains and routes connections through the private endpoint. The driver never resolves routing table hostnames via public DNS, so connections never bypass the tunnel.

2. **TLS SNI.** Each domain in the PE rule becomes the SNI hostname for connections to that domain. Because the Application Gateway operates in L4 TCP passthrough, the SNI reaches Aura untouched. Aura's wildcard certificate (`*.production-orch-*.neo4j.io`) covers all routing table hostnames, so certificate validation succeeds for every member connection.

### Validation Results (2026-03-20)

TLS certificates were verified first via `openssl s_client` against each routing table hostname as the SNI. Aura's wildcard certificate covered all three members with `Verify return code: 0 (ok)`.

The PE rule was updated from one domain to four using `deploy.py update-pe-domains`. After five minutes of propagation, a Databricks serverless notebook connected with `neo4j+s://` through the full Private Link chain:

- The driver fetched the routing table (three routers, two readers, one writer)
- Connections to routing table member hostnames were routed through the private endpoint
- Read and write queries executed successfully through the tunnel
- TLS SNI was preserved end-to-end through the L4 TCP passthrough

This confirmed that the NCC multi-domain approach enables the full `neo4j+s://` protocol with zero infrastructure changes.

## Phased Deployment

Application Gateway v2's Private Link feature has a validation gap with L4 TCP listeners.

When a private endpoint is created against an Application Gateway, Azure validates the gateway's Private Link configuration by inspecting its `httpListeners`. L4 TCP listeners (the `listeners` property in the ARM/Bicep schema) are invisible to this validation. If both L7 and L4 listeners exist when the private endpoint is created, the validation fails with a misleading error:

```
ApplicationGatewayPrivateLinkOperationError:
Cannot perform private link operation on ApplicationGateway ...
Please make sure application gateway has private link configuration.
```

The Private Link configuration is present and correctly bound. The failure is a validation bug: the code path that checks PL readiness only inspects L7 listeners.

### The Two-Phase Workaround

**Phase 1** deploys a pure L7 gateway with an HTTP listener on port 80 and a Private Link configuration bound to the frontend IP. The HTTP listener exists solely to satisfy PL validation. The private endpoint is created and approved during this phase. Backend health will show "Unhealthy" because Aura BC does not speak HTTP; this is expected and irrelevant.

**Phase 2** updates the same gateway to add the L4 TCP listener on port 7687 with the Aura BC FQDN as the backend. Azure does not re-validate the Private Link configuration on gateway updates. The private endpoint tunnel, already established, continues forwarding traffic. The L4 listener begins proxying TCP connections to Aura BC immediately.

This is a one-time setup step, not an ongoing operational concern. Once the private endpoint is established, the gateway can be updated freely without re-triggering PL validation. The Bicep templates in `infra/main-phase1.bicep` and `infra/main-phase2.bicep` encode this split, and `setup_azure.py` orchestrates the two phases.

## Protocol Support

With the NCC multi-domain PE rule in place, both Neo4j connection protocols work through the tunnel:

**`neo4j+s://` (routing protocol)** is the standard Neo4j connection scheme. The driver discovers cluster members via the routing table and distributes queries across readers and writers. With all routing table hostnames in the PE rule, every connection is routed through the private endpoint. This enables client-side routing, read/write splitting, and automatic failover across cluster members.

**`bolt+s://` (direct protocol)** establishes a single connection to a single server with no routing table discovery. It works with a single-domain PE rule (just the connection FQDN). This mode sacrifices client-side routing and failover but is simpler to configure and was the only option before the multi-domain PE rule was validated.

For production workloads, `neo4j+s://` with the multi-domain PE rule is the recommended configuration because it provides the full Neo4j protocol feature set.

## Constraints

| Constraint | Detail |
|------------|--------|
| Multi-domain PE rule required for `neo4j+s://` | The routing table member hostnames must be included in the NCC PE rule's `domain_names` array. Without them, routing table connections bypass the tunnel. Use `deploy.py update-pe-domains` to automate. For `bolt+s://` (direct mode, no routing), only the connection FQDN is needed. |
| Real Aura FQDN required as NCC domain | Databricks uses each PE rule domain as the TLS SNI hostname. Aura BC rejects connections where the SNI does not match its certificate. Custom private domains do not work. |
| ~300s idle timeout | Azure Private Link enforces an idle timeout of approximately five minutes. The Neo4j driver must set `max_connection_lifetime` and `liveness_check_timeout` below 300 seconds to prevent silent connection drops. |
| NCC region must match workspace region | The NCC must be created in the same Azure region as the Databricks workspace. The Application Gateway can be in a different region. |
| Phased deployment required | L4 TCP listeners must be added after PE creation. Do not deploy both listener types and create the PE in a single step. |
| Public internet leg remains | Traffic from the Application Gateway to Aura BC traverses the public internet over TLS. Only Aura VDC with native Azure Private Link eliminates this hop entirely. |
| Routing table hostname stability | The routing table member hostnames appear stable across the lifetime of an Aura BC instance but could change during scaling or maintenance events. If they change, re-run `deploy.py update-pe-domains`. For production, consider automating this on a schedule. |

## Alternatives Evaluated

Several approaches were investigated before arriving at the Application Gateway architecture. Each is documented here with the reason it was set aside.

### Load Balancer with HAProxy Reverse Proxy

**Status:** Works, but not preferred.

This approach deploys an Internal Load Balancer fronted by a Private Link Service, with an HAProxy VM forwarding Bolt traffic to Aura BC via a NAT Gateway. It was validated end-to-end with `bolt+s://`.

The Application Gateway approach is preferred because it eliminates the reverse proxy entirely. The LB architecture requires four components (ILB, PLS, HAProxy VM, NAT Gateway) where the Application Gateway requires one managed resource. The VM introduces a single point of failure, ongoing patching and monitoring requirements, and kernel-level packet forwarding overhead. The NAT Gateway adds cost for a static outbound IP. The Application Gateway provides load balancing, traffic forwarding, and a static outbound IP natively. Traffic traverses fewer network hops, which yields lower end-to-end latency.

The LB architecture remains a valid fallback if the Application Gateway phased deployment is incompatible with a customer's change management process. The NCC multi-domain PE rule should work identically with either architecture since NCC sits in front of both, though it has only been validated on the Application Gateway path.

### Dual Load Balancers Splitting by Port

**Status:** Not viable.

This approach proposed splitting Bolt traffic (port 7687) and HTTP API traffic (port 7473) across two separate load balancers. The reasoning was that `neo4j+s://` routing table discovery used the HTTP API on a separate port. Investigation revealed that the routing table is fetched over the Bolt protocol on port 7687, not the HTTP API. Port 7473 is not involved in `neo4j+s://` routing behavior. Two load balancers carrying the same port to the same destination would add cost and complexity without solving the hostname resolution problem.

### SNI-Based Reverse Proxy Port Routing

**Status:** Not viable.

This approach proposed using HAProxy's SNI inspection to route traffic between Bolt and HTTP ports based on the TLS hostname. It was based on the same incorrect assumption as the dual load balancer approach: that routing table discovery required the HTTP API on port 7473. Since the ROUTE message is a Bolt protocol message on port 7687, SNI-based port routing was addressing a non-existent problem.

### Self-Hosted Reverse Proxy Pattern Applied to Managed Service

**Status:** Not transferable.

A demonstration with a self-hosted Neo4j cluster showed `neo4j+s://` working through an HAProxy reverse proxy with round-robin backend routing. That setup worked because the operator controlled all hostnames, DNS records, and certificates, and could configure HAProxy backends to match the routing table entries.

Aura BC is a managed service. The routing table returns hostnames in a domain the customer does not control (`*.production-orch-*.neo4j.io`), and these hostnames differ from the connection FQDN (`*.databases.neo4j.io`). The proxy mechanism works identically in TCP passthrough mode, but the DNS and hostname control that made the self-hosted pattern viable does not exist with a managed service. The NCC multi-domain PE rule solves this at the network layer instead.

### Direct Databricks Serverless IP Allowlisting

**Status:** Not viable without preview feature access.

Databricks serverless compute does not expose stable outbound IP addresses. The IP pool changes as compute scales. A serverless compute firewall preview feature exists that would provide the actual outbound IP list for allowlisting on Aura BC, eliminating the need for Private Link entirely. Access to this feature has not been granted.

### NCC Wildcard Domain Rules

**Status:** Not supported by NCC.

NCC does not support wildcard patterns in PE rule domain names. Only exact FQDNs are accepted. A wildcard rule like `*.production-orch-*.neo4j.io` would have matched all routing table hostnames with a single entry, but the NCC API rejects non-FQDN values. The multi-domain PE rule (explicit listing of all FQDNs) is the working alternative.

### Neo4j Driver Custom Resolver

**Status:** Insufficient coverage.

The Neo4j driver offers a custom resolver interface that can override hostname resolution. Investigation revealed that no driver implementation intercepts the full connection lifecycle:

- The Java driver resolver only intercepts the initial seed address. Routing table addresses bypass the resolver entirely.
- The Python driver resolver intercepts routing table router contacts but not reader/writer query connections.
- The Spark connector exposes no custom resolver interface.

Even in the best case, the resolver cannot control the network path for actual query traffic to cluster members. The NCC multi-domain approach solves DNS interception at the network layer, making driver-level resolution unnecessary.

## Glossary

- **Aura BC (Aura Business Critical):** Neo4j's fully managed graph database tier. Runs in Neo4j's own Azure subscription. Supports IP allowlisting but not native Private Link (that requires Aura VDC).
- **Aura VDC (Virtual Dedicated Cloud):** Neo4j's highest tier with native Private Link support, eliminating the need for the intermediary architectures in this repo.
- **Bolt:** The binary protocol Neo4j uses for client-to-server communication on port 7687. `bolt+s://` is Bolt over TLS in direct mode (one connection, one server). `neo4j+s://` is Bolt over TLS with routing (the driver discovers cluster members and opens multiple connections).
- **FQDN (Fully Qualified Domain Name):** The complete hostname of a service, e.g., `f5919d06.databases.neo4j.io`.
- **HAProxy:** Open-source software that acts as a TCP/HTTP proxy. Used in the LB approach as a reverse proxy VM that forwards Bolt traffic from the load balancer to Aura BC.
- **L4 / L7 (Layer 4 / Layer 7):** Networking layers. L4 (transport) works with raw TCP connections without inspecting the content. L7 (application) understands HTTP and can make routing decisions based on URLs, headers, etc. Both approaches here operate at L4 because Bolt is not HTTP.
- **NAT Gateway:** An Azure resource that gives VMs a static public IP for outbound internet traffic. Needed in the LB approach so that the proxy VM's outbound IP is predictable and can be added to Aura BC's IP allowlist.
- **NCC (Network Connectivity Configuration):** A Databricks account-level resource that controls how serverless compute connects to external services. Private endpoint rules inside an NCC route traffic through Private Link instead of the public internet.
- **PLS (Private Link Service):** An Azure resource that accepts incoming Private Endpoint connections and forwards them to a load balancer or application gateway. It is the "receiving end" of a Private Link connection.
- **Private Endpoint (PE):** A private IP address in a VNet that connects to a Private Link Service. Traffic between the PE and PLS stays on the Azure backbone network, never touching the public internet. Databricks NCC creates these automatically when you add a private endpoint rule.
- **Private Link:** Azure's mechanism for creating private, backbone-only connections between resources. Traffic between a Private Endpoint and a Private Link Service never leaves the Azure network. Not the same as a VPN or VNet peering.
- **SNI (Server Name Indication):** A TLS extension where the client tells the server which hostname it wants to connect to during the TLS handshake, before encryption starts. Aura BC uses SNI to route connections to the correct database instance. If a proxy or gateway strips or changes the SNI value, Aura BC rejects the connection.
- **TLS (Transport Layer Security):** Encryption protocol that secures data in transit. Both `bolt+s://` and `neo4j+s://` use TLS. The key concern in these architectures is whether intermediaries preserve the original TLS handshake or terminate and re-establish it.
- **VNet (Virtual Network):** An Azure virtual network. A private, isolated network segment in Azure where you deploy VMs, load balancers, and other resources. Resources inside a VNet can talk to each other over private IPs.
