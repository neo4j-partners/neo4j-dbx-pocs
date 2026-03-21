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

Second, all routing table hostnames resolve to the same IP address as the connection FQDN. At the DNS and network level, the different hostnames are routing labels, not different servers at different addresses. However, this does not mean the routing is meaningless. Aura's ingress layer reads the SNI from each incoming TLS connection and routes it to the specific cluster member that hostname represents. Connections with the `p-*-0001` SNI reach the writer (leader), while connections with `p-*-0002` or `p-*-0003` reach the readers (followers). The single IP is a shared front door; the actual routing to distinct backend members happens inside Aura based on the SNI value. This is why L4 TCP passthrough matters beyond just identifying the database instance: the SNI also determines which cluster member handles the connection.

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

The `deploy.py create-pe-rule` command handles this automatically during initial setup: it connects to Aura BC, fetches the routing table, extracts the member hostnames, and creates the PE rule with all four domains in a single POST call.

If routing table hostnames change later (during Aura maintenance or scaling), the `deploy.py update-pe-domains` command syncs the PE rule with the current routing table via a PATCH call. The NCC does not need to be detached from the workspace. Changes propagate in approximately five minutes. Running serverless compute should be restarted afterward to pick up the new DNS routing.

### Why This Works End-to-End

The multi-domain PE rule solves two problems simultaneously:

1. **DNS resolution.** NCC intercepts DNS for all four domains and routes connections through the private endpoint. The driver never resolves routing table hostnames via public DNS, so connections never bypass the tunnel.

2. **TLS SNI.** Each domain in the PE rule becomes the SNI hostname for connections to that domain. Because the Application Gateway operates in L4 TCP passthrough, the SNI reaches Aura untouched. Aura's wildcard certificate (`*.production-orch-*.neo4j.io`) covers all routing table hostnames, so certificate validation succeeds for every member connection.

### Validation Results (2026-03-20)

TLS certificates were verified first via `openssl s_client` against each routing table hostname as the SNI. Aura's wildcard certificate covered all three members with `Verify return code: 0 (ok)`.

The PE rule was created with all four domains (connection FQDN plus three routing table members). After five minutes of propagation, a Databricks serverless notebook connected with `neo4j+s://` through the full Private Link chain:

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

## bolt+s:// vs neo4j+s://

Both Neo4j connection protocols work through this architecture. They differ in complexity, capabilities, and what they require from the NCC configuration.

### bolt+s:// (Direct Mode)

The `bolt+s://` scheme establishes a single TLS connection to the server identified by the connection FQDN. The driver sends all queries over this one connection. There is no ROUTE message, no routing table discovery, and no additional connections to cluster members.

From a Private Link perspective, this is the simpler configuration. The NCC PE rule needs only one domain (the connection FQDN). TLS SNI is preserved through the L4 TCP passthrough, and Aura identifies the correct database instance from the SNI. The connection lands on whichever cluster member Aura's ingress assigns based on the connection FQDN.

What `bolt+s://` gives up: the driver has no awareness of the cluster topology. All queries, reads and writes alike, go to the same server over the same connection. There is no client-side read/write splitting, no load distribution across cluster members, and no automatic failover. If the connection drops, the driver must reconnect through the same FQDN.

### neo4j+s:// (Routing Mode)

The `neo4j+s://` scheme starts with the same initial connection but then sends a ROUTE message to fetch the cluster's routing table. The driver learns which members are routers, readers, and writers, and opens separate connections to each using their individual hostnames as the TLS SNI.

Because all routing table hostnames resolve to the same IP address, the routing may appear superficial at the network level. It is not. Aura's ingress layer reads the SNI on each connection and routes it to the specific cluster member that hostname represents. Connections with the writer's hostname reach the leader; connections with reader hostnames reach followers. The driver then directs write queries to the writer connection and read queries to reader connections, distributing load across the cluster.

This requires the NCC PE rule to contain all four domains (connection FQDN plus three routing table member hostnames). Without them, the driver resolves routing table hostnames via public DNS and connections bypass the tunnel.

### When Each Makes Sense

**`bolt+s://` is the right choice when simplicity matters more than cluster-level features.** A Databricks notebook running batch analytics, periodic ETL writes, or ad-hoc graph queries against Aura BC is typically issuing sequential operations from a single compute context. Read/write splitting provides no benefit when there is one caller making one type of query at a time. The simpler PE rule configuration and the absence of routing table hostname stability concerns make `bolt+s://` the lower-maintenance option.

**`neo4j+s://` is the right choice when the workload benefits from cluster topology awareness.** Applications with concurrent readers and writers, long-running services that need automatic failover if a cluster member becomes unavailable, or workloads where distributing reads across followers meaningfully reduces leader load all benefit from the routing protocol. The added NCC configuration complexity (four domains instead of one, potential hostname updates if the cluster changes) is justified by the operational capabilities.

For most Databricks serverless workloads, `bolt+s://` with a single-domain PE rule is likely sufficient. The multi-domain `neo4j+s://` configuration was validated to confirm it is possible and to provide the option when workload requirements demand it.

## Constraints

| Constraint | Detail |
|------------|--------|
| Multi-domain PE rule required for `neo4j+s://` | The routing table member hostnames must be included in the NCC PE rule's `domain_names` array. Without them, routing table connections bypass the tunnel. `deploy.py create-pe-rule` includes them automatically at setup. If hostnames drift, run `deploy.py update-pe-domains` to resync. For `bolt+s://` (direct mode, no routing), only the connection FQDN is needed. |
| Real Aura FQDN required as NCC domain | Databricks uses each PE rule domain as the TLS SNI hostname. Aura BC rejects connections where the SNI does not match its certificate. Custom private domains do not work. |
| ~300s idle timeout | Azure Private Link enforces an idle timeout of approximately five minutes. The Neo4j driver must set `max_connection_lifetime` and `liveness_check_timeout` below 300 seconds to prevent silent connection drops. |
| NCC region must match workspace region | The NCC must be created in the same Azure region as the Databricks workspace. The Application Gateway can be in a different region. |
| Phased deployment required | L4 TCP listeners must be added after PE creation. Do not deploy both listener types and create the PE in a single step. |
| Public internet leg remains | Traffic from the Application Gateway to Aura BC traverses the public internet over TLS. Only Aura VDC with native Azure Private Link eliminates this hop entirely. |
| Routing table hostname stability | The routing table member hostnames appear stable across the lifetime of an Aura BC instance but could change during scaling or maintenance events. If they change, re-run `deploy.py update-pe-domains`. For production, consider automating this on a schedule. |

