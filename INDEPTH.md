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


## Validated Result

On 2026-03-20, the NCC multi-domain approach was tested on the Application Gateway project. The existing PE rule was updated from one domain (the connection FQDN) to four domains (connection FQDN plus three routing table member hostnames) using the Databricks PATCH API. After five minutes of propagation, a serverless notebook connected with `neo4j+s://` and successfully:

- Fetched the routing table through the tunnel (three routers, two readers, one writer)
- Connected to routing table member hostnames through the private endpoint
- Executed read and write queries through the tunnel

The TLS certificate was verified beforehand via `openssl s_client`. Aura uses a wildcard certificate (`*.production-orch-1275.neo4j.io`, Let's Encrypt) that covers all routing table hostnames. The App Gateway L4 TCP listener passes raw bytes without terminating TLS, so the SNI reaches Aura untouched. Verify return code: 0.

This single PE rule update solved both the TLS SNI problem and the routing table hostname resolution problem with zero infrastructure changes.


## Current State of Each Project

**Application Gateway project** (`app-gateway-pl`): Working with `neo4j+s://`. Validated end-to-end on 2026-03-20. Uses Application Gateway v2 L4 TCP passthrough with Private Link. No VMs, no HAProxy, no NAT Gateway, no Load Balancer. The gateway handles TCP passthrough and preserves TLS SNI end-to-end. The NCC multi-domain approach enables the `neo4j+s://` protocol with client-side routing through the tunnel. The phased deployment (L7 first for PE validation, then add L4) is required because Azure has not integrated L4 TCP proxy validation with Private Link, but once established the PE continues forwarding without re-validation.

**Load Balancer project** (`aurabc-lb-validation`): Working with `bolt+s://`. Validated end-to-end on 2026-03-18. Uses ILB, HAProxy VM, NAT Gateway, and Private Link Service. The architecture is operationally heavier (VM patching, single point of failure, NAT Gateway cost). The NCC multi-domain approach should work identically here since NCC sits in front of both architectures, but has not been tested on this project. HAProxy in TCP passthrough mode preserves TLS SNI the same way the App Gateway does.

The App Gateway architecture is the preferred path because it eliminates the reverse proxy entirely. There are no VMs to maintain, no HAProxy configuration to manage, and no single point of failure at the proxy layer. Azure manages the gateway's availability and scaling. The phased deployment is a one-time setup step, not an ongoing operational concern.

Both projects share the same constraints: real Aura FQDN required as the NCC domain, approximately 300 second idle timeout from Private Link, NCC region must match workspace region, and traffic from the gateway to Aura still traverses the public internet.


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


## Validated Solution: NCC Multi-Domain PE Rule

Adding routing table hostnames to the NCC PE rule's `domain_names` array is the validated solution. A single PE rule supports up to 10 domains. The Aura BC routing table returns three member hostnames plus the connection FQDN, totaling four. The Databricks PATCH API updates domains on an existing PE rule without detaching the NCC.

**Solves TLS SNI:** Yes. The App Gateway L4 TCP listener (and HAProxy in the LB project) passes raw bytes without terminating TLS. The SNI reaches Aura untouched.
**Solves routing:** Yes. NCC intercepts DNS for all four domains and routes them through the private endpoint. The driver connects to routing table members through the tunnel.
**Difficulty:** One API call. Zero infrastructure changes.

The `deploy.py update-pe-domains` command automates this: it connects to Aura BC, fetches the routing table, and PATCHes the PE rule with all domains.

The remaining open question is hostname stability. The routing table member hostnames encode instance and member identifiers that appear stable for the lifetime of the Aura BC instance. If they change during scaling or maintenance, the `update-pe-domains` command can be re-run. For production, this could be automated on a schedule.


## Options Not Pursued

The following options were investigated but not tested because the NCC multi-domain approach solved the problem without requiring them.

**NCC Wildcard Domain Rule.** NCC does not support wildcard patterns. The documentation requires fully qualified domain names with exact matching only.

**Neo4j Driver Custom Resolver.** Research revealed that the Java driver resolver only intercepts the initial seed address, not routing table addresses. The Python driver resolver intercepts routing table router addresses but not reader/writer query connections. The Spark connector exposes no custom resolver interface. Even in the best case, the resolver cannot control the network path for actual query traffic. The NCC multi-domain approach solves DNS interception at the network layer, making the resolver unnecessary.

**Databricks Serverless Compute Firewall (Preview).** Would eliminate the need for Private Link entirely by providing Databricks outbound IPs for allowlisting. The feature remains in private preview with no access granted despite repeated requests.

**Aura VDC with Native Private Link.** The cleanest solution technically, but the customer does not want to pay for VDC.


## Ruled-Out Approaches

**Dual load balancers splitting port 7687 and port 7473.** The routing table is fetched over bolt (port 7687), not the HTTP API (port 7473). Splitting traffic by port does not address routing table hostname resolution. Two load balancers carrying the same port to the same destination adds cost and complexity with no benefit.

**SNI-based HAProxy routing between bolt and HTTP ports.** This was proposed based on the assumption that `neo4j+s://` needed the HTTP API on port 7473 for routing table discovery. It does not. The ROUTE message is a bolt protocol message sent over the same port 7687 connection. SNI routing between ports is solving a problem that does not exist.

**Load Balancer + HAProxy as the preferred architecture.** The LB project works, but the App Gateway approach eliminates the reverse proxy entirely. No VMs to patch, no HAProxy to configure, no NAT Gateway to maintain, no single point of failure at the proxy layer. The App Gateway is a fully managed Azure service with autoscaling. Both architectures preserve TLS SNI identically (TCP passthrough), and the NCC multi-domain approach works with either. The LB project remains a valid fallback if the App Gateway phased deployment becomes untenable, but the App Gateway is the preferred recommendation.

**Applying Guhan's self-hosted HAProxy pattern directly to Aura BC.** Guhan's demo used a self-hosted Neo4j cluster where he controlled all hostnames, DNS, and certificates. He configured HAProxy backends with round-robin to his own servers, and the routing table returned hostnames he had set up. Aura BC is a managed service. The routing table returns `p-*.production-orch-*.neo4j.io` hostnames that Neo4j controls, in a domain pattern that does not match the connection FQDN. The HAProxy configuration pattern works, but the DNS and hostname control that made it work in Guhan's environment does not translate to Aura BC without solving the NCC hostname resolution problem first.

**Direct IP allowlisting of Databricks serverless IP ranges on Aura BC.** Databricks serverless compute does not guarantee stable outbound IP addresses. The IP pool can change as compute scales up and down. Without the serverless compute firewall preview feature (which provides the actual IP list), there is no reliable set of IPs to allowlist.
