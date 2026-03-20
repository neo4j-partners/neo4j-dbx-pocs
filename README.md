# neo4j-dbx-pocs

Proof-of-concept architectures for connecting Azure Databricks serverless compute to Neo4j Aura Business Critical over Azure Private Link.

## Glossary

- **Aura BC (Aura Business Critical):** Neo4j's fully managed graph database tier. Runs in Neo4j's own Azure subscription, not yours. Supports IP allowlisting but not native Private Link (that requires Aura VDC).
- **Aura VDC (Virtual Dedicated Cloud):** Neo4j's highest tier. Runs in a dedicated Azure subscription with native Private Link support, eliminating the need for any of the workarounds in this repo.
- **Bolt:** The binary protocol Neo4j uses for client-to-server communication. Runs on port 7687 over TCP. `bolt+s://` is Bolt over TLS in direct mode (one connection, one server). `neo4j+s://` is Bolt over TLS with routing (the driver discovers backend servers and opens multiple connections).
- **FQDN (Fully Qualified Domain Name):** The complete hostname of a service, like `f5919d06.databases.neo4j.io`. Used here because Databricks and Aura BC both need the real FQDN for TLS to work correctly.
- **HAProxy:** Open-source software that acts as a TCP/HTTP proxy. Used in the LB approach as a reverse proxy VM that forwards Bolt traffic from the load balancer to Aura BC.
- **L4 / L7 (Layer 4 / Layer 7):** Networking layers. L4 (transport layer) works with raw TCP connections without inspecting the content. L7 (application layer) understands HTTP and can make routing decisions based on URLs, headers, etc. Both approaches here operate at L4 because Bolt is not HTTP.
- **NAT Gateway:** An Azure resource that gives VMs a static public IP for outbound internet traffic. Needed in the LB approach so that the proxy VM's outbound IP is predictable and can be added to Aura BC's IP allowlist.
- **NCC (Network Connectivity Configuration):** A Databricks account-level resource that controls how serverless compute connects to external services. You create private endpoint rules inside an NCC to route traffic through Private Link instead of the public internet.
- **PLS (Private Link Service):** An Azure resource that accepts incoming Private Endpoint connections and forwards them to a load balancer or application gateway. It is the "receiving end" of a Private Link connection.
- **Private Endpoint (PE):** A private IP address in a VNet that connects to a Private Link Service. Traffic between the PE and PLS stays on the Azure backbone network, never touching the public internet. Databricks NCC creates these automatically when you add a private endpoint rule.
- **Private Link:** Azure's mechanism for creating private, backbone-only connections between resources. Traffic between a Private Endpoint and a Private Link Service never leaves the Azure network. Not the same as a VPN or VNet peering.
- **SNI (Server Name Indication):** A TLS extension where the client tells the server which hostname it wants to connect to during the TLS handshake, before encryption starts. Aura BC uses SNI to route connections to the correct database instance. If a proxy or gateway strips or changes the SNI value, Aura BC rejects the connection.
- **TLS (Transport Layer Security):** Encryption protocol that secures data in transit. Both `bolt+s://` and `neo4j+s://` use TLS. The key concern in these architectures is whether intermediaries (load balancers, gateways) preserve the original TLS handshake or terminate and re-establish it.
- **VNet (Virtual Network):** An Azure virtual network. A private, isolated network segment in Azure where you deploy VMs, load balancers, and other resources. Resources inside a VNet can talk to each other over private IPs.

## How Aura BC handles connections

When you connect to an Aura BC instance, you are not connecting directly to a single database server. The FQDN you are given (e.g. `f5919d06.databases.neo4j.io`) resolves to a shared ingress endpoint that serves many customers and many database instances. Aura uses the SNI value in the TLS handshake to determine which database instance the connection is for. The client says "I want to talk to `f5919d06.databases.neo4j.io`" during the TLS handshake, and Aura's ingress layer reads that hostname and routes the connection to the correct backend cluster.

This is why SNI preservation is critical in both approaches. If a load balancer, gateway, or proxy terminates the TLS connection and opens a new one to Aura, it must send the correct SNI value on the new connection. If it sends a different hostname, or no hostname, Aura has no way to know which database instance the connection belongs to and rejects it. Both approaches in this repo handle this by operating in TCP passthrough mode, where the original TLS handshake from the client passes through untouched and Aura sees the real SNI value.

This also explains why the NCC private endpoint rule must use the real Aura FQDN as its domain. Databricks uses that domain as the SNI hostname when it initiates the TLS handshake through the private endpoint. If you set the domain to something else (like a custom private DNS name), Databricks sends that custom name as the SNI, Aura does not recognize it, and the connection fails.

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
