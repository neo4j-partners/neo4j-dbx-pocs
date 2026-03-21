# Private Link to Aura BC: Routing Table Findings and Path Forward

## What the Routing Table Investigation Revealed

A prototype script connected directly to Aura Business Critical using the `neo4j+s://` protocol and inspected the routing table that the driver receives after its initial connection. The results reframe the entire problem.

The routing table returned three cluster member hostnames, all on port 7687:

```
Routers:
  p-8cc8f63c-365a-0003.production-orch-1275.neo4j.io:7687
  p-8cc8f63c-365a-0002.production-orch-1275.neo4j.io:7687
  p-8cc8f63c-365a-0001.production-orch-1275.neo4j.io:7687

Readers:
  p-8cc8f63c-365a-0002.production-orch-1275.neo4j.io:7687
  p-8cc8f63c-365a-0003.production-orch-1275.neo4j.io:7687

Writers:
  p-8cc8f63c-365a-0001.production-orch-1275.neo4j.io:7687
```

Three facts matter.

First, every entry uses port 7687. The routing table is fetched over the bolt connection itself using a ROUTE message in the bolt protocol. The HTTP API on port 7473 is not involved. The dual-port framing in earlier analysis was wrong. The driver does not need to reach port 7473 for routing table discovery.

Second, the hostnames are in a completely different domain from the connection FQDN. The driver connects to `8cc8f63c.databases.neo4j.io`, but the routing table returns `p-8cc8f63c-365a-000X.production-orch-1275.neo4j.io`. These are not subdomains of the connection address. They follow a different naming convention entirely.

Third, all three routing hostnames resolve to the same IP address as the connection FQDN: `20.127.122.152`. Aura BC runs all cluster member traffic through a single edge endpoint and uses the TLS SNI hostname to determine which member should handle the connection. The different hostnames are routing labels, not different servers at different addresses.

The routing table TTL is 10 seconds, meaning the driver refreshes its member list frequently. The hostnames themselves appear stable for the lifetime of the instance; it is the role assignments (which member is reader, writer, router) that change.


## What This Changes About Prior Assumptions

Earlier analysis in HAPROXY.md framed the problem as a dual-port challenge. The assumption was that the `neo4j+s://` driver needed to reach two different services: bolt on port 7687 for queries and the HTTP API on port 7473 for routing table discovery. The proposed solutions (SNI-based HAProxy routing between ports, dual load balancers splitting traffic by port) followed from this framing.

That framing was incorrect. The routing table is fetched over bolt, not HTTP. Port 7473 is irrelevant to the `neo4j+s://` protocol's routing behavior. The problem is not about multiplexing two ports through one tunnel. It is about resolving three hostnames that the tunnel does not know about.

Guhan's HAProxy demo showed `neo4j+s://` working through a proxy, but that demo used a self-hosted Neo4j cluster where he controlled every hostname and could map them in HAProxy's backend configuration and DNS. Aura BC is a managed service. Neo4j controls the FQDNs in the routing table, and those FQDNs do not match the connection address that the NCC private endpoint rule is configured for.

The dual-LB strategy from HUM.md proposed splitting port 7687 and port 7473 across two load balancers. Since the routing table does not use port 7473, this split serves no purpose for enabling `neo4j+s://`. Two load balancers carrying the same port to the same destination adds infrastructure without solving the hostname resolution problem.


## The Actual Problem

When Databricks serverless uses the `neo4j+s://` protocol, the following sequence occurs:

1. The driver resolves `8cc8f63c.databases.neo4j.io`. The NCC private endpoint rule matches this domain and routes the DNS lookup through the private endpoint. The connection flows through the tunnel: PE, PLS, ILB, HAProxy, NAT Gateway, Aura BC.

2. Over this bolt connection, the driver sends a ROUTE message. Aura responds with the routing table containing three `p-*.production-orch-1275.neo4j.io` hostnames.

3. The driver attempts to connect to one of these routing hostnames. Databricks resolves the hostname. The NCC private endpoint rule only matches `8cc8f63c.databases.neo4j.io`. The routing hostname does not match. NCC does not intercept the DNS lookup.

4. The hostname resolves to its public IP (20.127.122.152). The driver connects directly to Aura BC from a Databricks serverless IP address. Aura's IP allowlist contains only the NAT Gateway IP, not the Databricks serverless IP pool. Aura rejects the connection.

The result: the initial connection through the tunnel succeeds, but every subsequent connection to a routing table member fails. The driver falls back or errors out. This is why both projects require `bolt+s://`, which skips the ROUTE message entirely and sends all queries down the original connection.

The fix requires one of two things. Either the routing table hostnames must resolve through the private endpoint (an NCC-level change), or the driver must be configured to override hostname resolution so that routing table entries map to the tunnel endpoint (a driver-level change). Both paths need investigation.

HAProxy itself is not the bottleneck. In TCP passthrough mode, HAProxy forwards raw bytes without inspecting or modifying TLS. Whatever SNI the driver sends in its ClientHello passes through to Aura untouched. Since all routing hostnames resolve to the same Aura edge IP and Aura uses SNI to route to the correct member, the proxy chain works as long as the driver's connection arrives at the proxy in the first place. The question is whether Databricks can be made to send those connections through the tunnel rather than directly to the public internet.


## Current State of Each Project

**Load Balancer project** (`aurabc-lb-validation`): Working. Validated end-to-end on 2026-03-18. Uses `bolt+s://` through ILB, HAProxy VM, NAT Gateway, and Private Link Service. The architecture is operationally heavy (VM patching, single point of failure, NAT Gateway cost) but stable.

**Application Gateway project** (`app-gateway-pl`): Working with a caveat. Validated end-to-end on 2026-03-20 using a phased deployment workaround. Azure's L4 TCP proxy on Application Gateway v2 and Private Link were developed independently; PE creation only validates L7 HTTP listeners. The workaround deploys a pure L7 gateway first (so PE validation passes), then updates the gateway to add L4 listeners. The established PE continues forwarding traffic without re-validation. This works today but depends on Azure not closing the validation gap. It is not a defensible customer recommendation.

Both projects share the same constraints: `bolt+s://` only, real Aura FQDN required as the NCC domain, approximately 300 second idle timeout from Private Link, NCC region must match workspace region, and traffic from the proxy to Aura still traverses the public internet.


## Outstanding Questions

### NCC and Databricks

**Does NCC support wildcard domain rules?** If NCC allows a rule like `*.production-orch-1275.neo4j.io` or `*.neo4j.io`, all routing table hostnames would match and route through the private endpoint. This is the single most important question. If the answer is yes, `neo4j+s://` works with minimal infrastructure change.

**Can NCC have multiple domain rules pointing to the same Private Link Service?** If wildcards are not supported, individual rules for each routing hostname would work. The routing hostnames appear stable (they encode instance and member IDs), but this needs confirmation. If hostnames change on cluster scaling or rebalancing, hardcoded rules become stale.

**Does NCC match domains exactly or by suffix?** The NCC documentation describes domain-based rules but does not specify the matching behavior. Exact match, prefix match, and suffix match would each have different implications for whether routing table hostnames can be captured.

**Is the Databricks serverless compute firewall preview accessible?** This is the feature Ryan mentioned in the March 20 call that provides a JSON file containing Databricks serverless outbound IP addresses. If enabled, those IPs can be allowlisted on Aura BC directly, eliminating the need for Private Link infrastructure entirely. `neo4j+s://` would work natively. Repeated requests to the Databricks partner team have not resulted in access.

### Neo4j Driver and Aura

**Does the Neo4j Spark connector expose a custom address resolver?** The Neo4j Python driver accepts a `resolver` parameter that overrides hostname resolution. If the Spark connector (which wraps the Java driver) exposes an equivalent option, the driver could resolve all routing table hostnames to the private endpoint IP. This would bypass the NCC domain matching problem entirely.

**Are the routing table hostnames stable across the lifetime of an Aura BC instance?** The hostnames encode what appears to be instance and member identifiers (`8cc8f63c`, `365a-0001`). If these remain constant, static NCC rules or DNS overrides are viable. If they change during scaling events, failovers, or maintenance, any static mapping breaks.

**Does the routing table structure differ across Aura BC configurations?** The prototype tested a single Aura BC instance. Different instance sizes, regions, or cluster configurations might return different hostname patterns or different numbers of members.

### Azure Infrastructure

**For the App Gateway project: will Azure integrate L4 TCP proxy with Private Link?** The L4 TCP proxy is in preview. Private Link is GA. The validation gap exists because these features were built independently. There is no public roadmap indicating when or whether integration will happen.


## Options Ranked by Difficulty

These options apply to the load balancer project. The application gateway project is blocked at a platform level and does not benefit from any of them; it should be set aside until Azure addresses the L4 and Private Link integration gap.

Each option is evaluated on two criteria: whether it solves the TLS SNI challenge (connections through the tunnel must present the correct SNI for Aura to accept them) and whether it enables client-side routing (the driver can connect to individual cluster members through the routing table).

### 1. NCC Wildcard Domain Rule

Add a wildcard domain rule (e.g. `*.neo4j.io` or `*.production-orch-1275.neo4j.io`) to the NCC private endpoint configuration. All routing table hostnames would match and route through the existing private endpoint to the PLS.

No infrastructure changes. HAProxy already passes through TLS in TCP mode, so the routing hostname SNI reaches Aura untouched. Aura's edge sees the correct member hostname and routes to the right cluster member.

**Solves TLS SNI:** Yes. SNI passes through HAProxy in TCP mode.
**Solves routing:** Yes. All routing hostnames resolve through the tunnel.
**Difficulty:** Lowest. One NCC configuration change.
**Risk:** NCC may not support wildcard domains. No documentation confirms this capability. If it does not, this option is unavailable.

### 2. Multiple NCC Domain Rules

Add each routing table hostname as a separate NCC private endpoint rule pointing to the same PLS. Three rules for three members, plus the original connection FQDN rule.

No infrastructure changes. Same SNI passthrough behavior as option 1.

**Solves TLS SNI:** Yes.
**Solves routing:** Yes, as long as the rules match the current routing hostnames.
**Difficulty:** Low. Configuration only.
**Risk:** The routing table hostnames must be known in advance and must remain stable. If Aura changes the hostnames during maintenance or scaling, the rules go stale and routing breaks silently. The 10-second routing table TTL means the driver checks frequently, but the hostnames themselves should change rarely. This approach is brittle for production but testable immediately.

### 3. Neo4j Driver Custom Resolver

Configure the Neo4j driver with a custom address resolver that maps all routing table hostnames to the private endpoint IP (or to the connection FQDN, which NCC already routes). The driver would resolve `p-8cc8f63c-365a-0001.production-orch-1275.neo4j.io` to the same address as `8cc8f63c.databases.neo4j.io`, and the connection would flow through the existing NCC rule and tunnel.

The Python driver supports this through the `resolver` parameter on `GraphDatabase.driver()`. The Java driver (used by the Spark connector) has a `ServerAddressResolver` interface. Whether the Neo4j Spark connector exposes this configuration is the open question.

**Solves TLS SNI:** Yes. The resolver overrides address resolution, but the TLS ClientHello still carries the original routing hostname as SNI. HAProxy passes it through.
**Solves routing:** Yes, if the Spark connector supports it.
**Difficulty:** Moderate. Requires confirming Spark connector capabilities, and the customer would need to configure this in their driver setup.
**Risk:** The Spark connector may not expose the resolver interface. Even if it does, the customer takes on the complexity of maintaining the resolver configuration.

### 4. Databricks Serverless Compute Firewall (Preview)

If the serverless compute firewall feature is enabled on the Databricks workspace, it provides a JSON file listing the outbound IP addresses used by serverless compute. Those IPs can be added to the Aura BC allowlist (using the Aura Admin API to keep them in sync). Private Link infrastructure becomes unnecessary. The driver connects directly to Aura BC over the public internet with `neo4j+s://`.

This eliminates the tunnel, the proxy, the load balancer, the NAT gateway, and every constraint associated with them. The `neo4j+s://` protocol works natively because the driver connects to Aura's public endpoint with all routing hostnames resolving normally.

**Solves TLS SNI:** Not applicable. No tunnel, no proxy, no SNI concerns.
**Solves routing:** Yes. Direct connectivity means the driver's routing table works as designed.
**Difficulty:** Zero infrastructure work, but the feature must be enabled by Databricks. Multiple requests have gone unanswered.
**Risk:** The feature is in private preview with no indication of when it becomes generally available. The IP list may be large or may change, requiring automated sync between the Databricks JSON file and the Aura BC allowlist API.

### 5. Aura VDC with Native Private Link

Aura Virtual Dedicated Cloud supports native Azure Private Link. Databricks creates a private endpoint directly to the Aura VDC instance. No proxy, no load balancer, no HAProxy. The `neo4j+s://` protocol works because the private endpoint connects directly to the Neo4j cluster, and routing table hostnames resolve within the private network.

**Solves TLS SNI:** Yes.
**Solves routing:** Yes.
**Difficulty:** None technically.
**Risk:** The customer has stated they do not want to pay for VDC. This remains the cleanest solution if cost constraints change.

### 6. Accept bolt+s:// Limitation

Use the existing validated architecture with `bolt+s://`. No routing table, no read distribution across cluster members, no transparent failover. All queries flow over a single connection to whichever member Aura's edge selects.

For workloads that are write-heavy or that do not require read scaling, this may be sufficient. The architecture is proven, and both projects have validated it end-to-end.

**Solves TLS SNI:** Yes. Single hostname, single connection.
**Solves routing:** No. This option accepts the limitation rather than solving it.
**Difficulty:** None. Already working.
**Risk:** None beyond the inherent limitations of single-endpoint connectivity.


## Ruled-Out Approaches

**Dual load balancers splitting port 7687 and port 7473.** The routing table is fetched over bolt (port 7687), not the HTTP API (port 7473). Splitting traffic by port does not address routing table hostname resolution. Two load balancers carrying the same port to the same destination adds cost and complexity with no benefit.

**SNI-based HAProxy routing between bolt and HTTP ports.** This was proposed based on the assumption that `neo4j+s://` needed the HTTP API on port 7473 for routing table discovery. It does not. The ROUTE message is a bolt protocol message sent over the same port 7687 connection. SNI routing between ports is solving a problem that does not exist.

**Application Gateway L4 + Private Link as a customer recommendation.** The phased deployment workaround exploits a validation gap where Azure does not re-validate Private Link configuration on gateway updates. This gap exists because L4 TCP proxy and Private Link were developed independently. Recommending an architecture that depends on Azure not fixing its own validation logic is not defensible.

**Application Gateway L4 + Private Link without phased deployment.** PE creation fails outright. Azure's Private Link validation only inspects L7 HTTP listeners. An L4-only gateway has no listeners that validation recognizes.

**Applying Guhan's self-hosted HAProxy pattern directly to Aura BC.** Guhan's demo used a self-hosted Neo4j cluster where he controlled all hostnames, DNS, and certificates. He configured HAProxy backends with round-robin to his own servers, and the routing table returned hostnames he had set up. Aura BC is a managed service. The routing table returns `p-*.production-orch-*.neo4j.io` hostnames that Neo4j controls, in a domain pattern that does not match the connection FQDN. The HAProxy configuration pattern works, but the DNS and hostname control that made it work in Guhan's environment does not translate to Aura BC without solving the NCC hostname resolution problem first.

**Direct IP allowlisting of Databricks serverless IP ranges on Aura BC.** Databricks serverless compute does not guarantee stable outbound IP addresses. The IP pool can change as compute scales up and down. Without the serverless compute firewall preview feature (which provides the actual IP list), there is no reliable set of IPs to allowlist.
