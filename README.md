
# neo4j-dbx-pocs

Proof-of-concept architecture for connecting Azure Databricks serverless compute to Neo4j Aura Business Critical over Azure Private Link.

## The Validated Solution: Application Gateway with Private Link

![Architecture Diagram](app-gateway-pl/architecture.png)

The [`app-gateway-pl/`](app-gateway-pl/) project is the validated and preferred approach. It uses Azure Application Gateway v2 as an L4 TCP proxy with Private Link to establish private connectivity from Databricks serverless compute to Neo4j Aura Business Critical. The architecture is fully managed: no VMs, no reverse proxies, no NAT Gateway.

Two mechanisms make this work:

- **L4 TCP passthrough preserves TLS SNI end-to-end.** Aura BC serves many database instances behind a single IP address and uses the TLS Server Name Indication (SNI) hostname to route each connection to the correct instance. The Application Gateway operates at Layer 4, forwarding raw TCP bytes without terminating TLS, so the SNI reaches Aura untouched. This is the foundation for all Bolt connectivity through the tunnel.

- **NCC multi-domain private endpoint rules enable `neo4j+s://` with full routing protocol support.** The `neo4j+s://` protocol triggers routing table discovery, where the driver fetches cluster member hostnames and opens separate connections to each. These routing table hostnames are in a completely different domain from the connection FQDN. By listing all hostnames (connection FQDN plus routing table members) in a single NCC PE rule, Databricks routes every connection through the Private Link tunnel. This enables client-side routing with read/write splitting across cluster members.

See [`app-gateway-pl/README.md`](app-gateway-pl/README.md) for deployment instructions and [`app-gateway-pl/ARCHITECTURE.md`](app-gateway-pl/ARCHITECTURE.md) for the full technical explanation.

## Preserving the Neo4j Protocol

A key goal of this architecture is preserving the full `neo4j+s://` routing protocol through the Private Link tunnel, not just establishing basic connectivity.

Neo4j's `neo4j+s://` protocol goes beyond a simple database connection. After the initial handshake, the driver discovers the cluster topology by fetching a routing table containing individual member hostnames for routers, readers, and writers. It then opens separate connections to each member, directing write queries to the leader and read queries to followers. This gives the client automatic read/write splitting, load distribution across cluster members, and failover if a member becomes unavailable.

Preserving this protocol through the Private Link tunnel delivers three advantages over a single-connection approach:

- **Read/write splitting.** The driver routes read queries to follower replicas and writes to the leader. This distributes load across the cluster rather than concentrating all traffic on one member.
- **Cluster-aware failover.** If a cluster member becomes unavailable, the driver refreshes its routing table and redirects traffic to healthy members without application-level intervention.
- **Load distribution.** Multiple reader connections spread query load across followers, reducing pressure on the leader and improving throughput for read-heavy workloads.

Plain `bolt+s://` can also be used with less complexity. It requires only the connection FQDN in the NCC PE rule (one domain instead of four) and skips routing table discovery entirely. This is simpler to configure and avoids any concern about routing table hostname stability, but all queries go to a single server with no client-side routing, no read/write splitting, and no automatic failover.

Both protocols were validated end-to-end through the Private Link tunnel on 2026-03-20. In both cases, the Application Gateway's L4 TCP passthrough preserves TLS SNI from the Neo4j driver all the way to Aura BC's ingress, where it is used to route the connection to the correct database instance and cluster member.

## Operational Monitoring

The architecture depends on external state it does not control: the hostnames in Aura BC's routing table, the NCC private endpoint connection state, and the TLS certificate Aura serves on its shared ingress. Any of these can change without warning, and when they do, the failure mode is silent — connections time out or get rejected with no alarm.

A lightweight Azure Function running on a timer trigger can monitor these dependencies, automatically sync routing table hostname drift via the existing `deploy.py update-pe-domains` logic, and alert when manual intervention is required. See [`app-gateway-pl/AZURE_FUNCTION.md`](app-gateway-pl/AZURE_FUNCTION.md) for the full proposal. This has not been implemented, however it should be trivial given that the monitoring checks reuse the same Aura and Databricks API patterns already proven in the deployment scripts.

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

Several alternative architectures were investigated and ruled out during this project. See [`docs/alternatives.md`](docs/alternatives.md) for full details on each approach and why it was set aside.

## Glossary

See [`docs/glossary.md`](docs/glossary.md) for definitions of all terms and acronyms used across this project.
