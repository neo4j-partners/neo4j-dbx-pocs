# neo4j-dbx-pocs

Proof-of-concept architectures for connecting Azure Databricks serverless compute to Neo4j Aura Business Critical over Azure Private Link.

## The Validated Solution: Application Gateway with Private Link

The [`app-gateway-pl/`](app-gateway-pl/) project is the validated and preferred approach. It uses Azure Application Gateway v2 as an L4 TCP proxy with Private Link to establish private connectivity from Databricks serverless compute to Neo4j Aura Business Critical. The architecture is fully managed: no VMs, no reverse proxies, no NAT Gateway.

Two mechanisms make this work:

- **L4 TCP passthrough preserves TLS SNI end-to-end.** Aura BC serves many database instances behind a single IP address and uses the TLS Server Name Indication (SNI) hostname to route each connection to the correct instance. The Application Gateway operates at Layer 4, forwarding raw TCP bytes without terminating TLS, so the SNI reaches Aura untouched. This is the foundation for all Bolt connectivity through the tunnel.

- **NCC multi-domain private endpoint rules enable `neo4j+s://` with full routing protocol support.** The `neo4j+s://` protocol triggers routing table discovery, where the driver fetches cluster member hostnames and opens separate connections to each. These routing table hostnames are in a completely different domain from the connection FQDN. By listing all hostnames (connection FQDN plus routing table members) in a single NCC PE rule, Databricks routes every connection through the Private Link tunnel. This enables client-side routing with read/write splitting across cluster members.

```
Databricks Serverless (eastus)
    |
    |  NCC Private Endpoint (multi-domain PE rule)
    v
Private Link tunnel (Azure backbone)
    |
    v
Application Gateway v2 (L4 TCP, port 7687, TLS passthrough)
    |
    v
Neo4j Aura Business Critical
```

See [`app-gateway-pl/README.md`](app-gateway-pl/README.md) for deployment instructions and [`app-gateway-pl/ARCHITECTURE.md`](app-gateway-pl/ARCHITECTURE.md) for the full technical explanation.

## Next Step: Direct Databricks Serverless IP Allowlisting

The Application Gateway solution works today, but a simpler path may be possible. Databricks has a serverless compute firewall preview feature that provides the outbound IP addresses used by serverless compute. If enabled, those IPs can be allowlisted directly on Aura BC's IP allowlist, eliminating the need for Private Link infrastructure entirely. The Neo4j driver would connect to Aura BC over the public internet using `neo4j+s://` with full routing protocol support, no intermediary gateway or proxy required.

We would like to work with the Databricks team to prototype this approach. Access to the serverless compute firewall preview has not yet been granted.

## Why Application Gateway Over Load Balancer

An alternative approach using an Internal Load Balancer with an HAProxy reverse proxy VM was also validated (see [`aurabc-lb-validation/`](aurabc-lb-validation/)). Both architectures produce a working Private Link path to Aura BC, but the Application Gateway is preferred for both operational and performance reasons.

The load balancer approach routes traffic through more intermediate components: from the Private Link Service to the Internal Load Balancer, then to the HAProxy VM, and finally through a NAT Gateway before reaching the public internet. Each hop adds network latency. The HAProxy VM, even in TCP passthrough mode, introduces kernel-level packet forwarding overhead and represents a single point of failure that must be patched, monitored, and kept running.

The Application Gateway consolidates this entire chain into a single managed resource. Traffic flows from the Private Link tunnel to the gateway, which connects directly to Aura BC via its own public IP. Fewer network hops and no VM processing overhead yield lower end-to-end latency. The gateway is also zone-redundant by default, eliminating the availability concern that a single HAProxy VM presents.

Beyond performance, the operational difference is significant. The load balancer approach requires maintaining a VM (OS patching, HAProxy configuration, process monitoring), provisioning a NAT Gateway for a static outbound IP, and managing a separate Private Link Service resource. The Application Gateway handles all of this as a managed service with a single Azure resource.

| | Application Gateway | Load Balancer + HAProxy |
|---|---|---|
| Infrastructure components | 1 (App Gateway) | 4 (PLS, ILB, HAProxy VM, NAT GW) |
| VM management | None | HAProxy VM (patching, monitoring) |
| Outbound IP | Gateway's own public IP | NAT Gateway required |
| Availability | Zone-redundant by default | Single VM (unless clustered) |
| TLS SNI preservation | L4 TCP passthrough | HAProxy TCP passthrough |
| `neo4j+s://` support | Validated with multi-domain PE rule | Expected (not yet tested on this architecture) |

## Approaches That Did Not Work

Several alternative architectures were investigated and ruled out during this project.

**Dual load balancers splitting traffic by port.** Based on the assumption that `neo4j+s://` routing table discovery used the HTTP API on port 7473 in addition to Bolt on port 7687. Investigation revealed that the routing table is fetched entirely over the Bolt protocol on port 7687. Port 7473 is not involved. Splitting traffic by port added infrastructure without addressing the actual hostname resolution problem.

**SNI-based reverse proxy port routing.** Proposed using HAProxy's SNI inspection to route traffic between Bolt and HTTP ports based on the TLS hostname. Built on the same incorrect port assumption as the dual load balancer approach. Since the ROUTE message is a Bolt protocol message on port 7687, SNI-based port routing was addressing a non-existent problem.

**Self-hosted reverse proxy pattern applied to a managed service.** A demonstration with a self-hosted Neo4j cluster showed `neo4j+s://` working through an HAProxy reverse proxy. That setup relied on controlling all hostnames, DNS records, and certificates, and could configure HAProxy backends to match the routing table entries. Aura BC is a managed service where the routing table returns hostnames in a different domain from the connection FQDN, and the customer has no control over DNS or certificates. The proxy mechanism works, but the hostname control that made the self-hosted pattern viable does not exist with Aura BC.

**NCC wildcard domain rules.** NCC private endpoint rules do not support wildcard patterns. Only exact FQDNs are accepted. A wildcard matching all routing table hostnames would have been the simplest solution, but the API rejects non-FQDN values.

**Neo4j driver custom resolver.** The Java driver resolver only intercepts the initial seed address, not routing table member addresses. The Python driver resolver covers routing table contacts but not reader/writer query connections. The Spark connector exposes no resolver interface. None provided sufficient coverage to redirect all traffic through the tunnel.

## Glossary

- **Aura BC (Aura Business Critical):** Neo4j's fully managed graph database tier. Runs in Neo4j's own Azure subscription. Supports IP allowlisting but not native Private Link (that requires Aura VDC).
- **Aura VDC (Virtual Dedicated Cloud):** Neo4j's highest tier with native Private Link support, eliminating the need for the intermediary architectures in this repo.
- **Bolt:** The binary protocol Neo4j uses for client-to-server communication on port 7687. `bolt+s://` is Bolt over TLS in direct mode (one connection, one server). `neo4j+s://` is Bolt over TLS with routing (the driver discovers cluster members and opens multiple connections).
- **FQDN (Fully Qualified Domain Name):** The complete hostname of a service, e.g., `f5919d06.databases.neo4j.io`.
- **L4 / L7 (Layer 4 / Layer 7):** Networking layers. L4 (transport) works with raw TCP connections. L7 (application) understands HTTP. Both approaches here operate at L4 because Bolt is not HTTP.
- **NCC (Network Connectivity Configuration):** A Databricks account-level resource that controls how serverless compute connects to external services. Private endpoint rules inside an NCC route traffic through Private Link.
- **PLS (Private Link Service):** An Azure resource that accepts incoming Private Endpoint connections and forwards them to a load balancer or application gateway.
- **Private Endpoint (PE):** A private IP address in a VNet that connects to a Private Link Service. Traffic stays on the Azure backbone.
- **Private Link:** Azure's mechanism for private, backbone-only connections between resources.
- **SNI (Server Name Indication):** A TLS extension where the client declares the target hostname during the handshake, before encryption begins. Aura BC uses SNI to route connections to the correct database instance.
- **TLS (Transport Layer Security):** Encryption protocol securing data in transit. The key concern in these architectures is whether intermediaries preserve the original TLS handshake or terminate and re-establish it.
- **VNet (Virtual Network):** An Azure virtual network where resources are deployed and communicate over private IPs.
