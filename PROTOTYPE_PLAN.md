# Prototype Plan: Enabling neo4j+s:// Through the Private Link Tunnel

## What the Research Revealed

Three sub-agents investigated the Databricks NCC documentation, the Neo4j driver resolver APIs across Python and Java, and the Spark connector's driver configuration surface. The findings reshape which options are viable and how each prototype should be structured.

### NCC Domain Rules

NCC private endpoint rules require fully qualified domain names. No wildcard patterns, no suffix matching, no glob syntax. The documentation states that domain names must "resolve directly to the backend resources," and every example uses explicit FQDNs. This eliminates the wildcard domain approach entirely.

However, a single PE rule supports up to 10 domain names as an array. The routing table from our Aura BC instance contains three member hostnames plus the connection FQDN, totaling four domains. They fit comfortably in one rule. The NCC API exposes a PATCH endpoint that can update `domain_names` on an existing PE rule without detaching the NCC from the workspace. Changes take approximately five minutes to propagate, and running serverless compute should be restarted afterward.

### Neo4j Driver Resolver

The resolver behaves differently across driver implementations, and neither covers the full connection lifecycle.

The Java driver's `ServerAddressResolver` is explicitly documented as applying only to the initial seed address: "addresses that the driver receives in routing tables are not resolved with the custom resolver." This means the resolver handles the first connection but every subsequent connection to a routing table member uses standard DNS resolution. The resolver cannot redirect reader or writer traffic through the tunnel.

The Python driver's `resolver` parameter has broader reach. Source code analysis confirms it is called for routing table router addresses during table refresh. But it is not called when the driver connects to readers or writers for actual query execution. Those connections go through standard DNS. So even with a Python resolver, the driver's query traffic to cluster members would bypass the tunnel.

The Neo4j Spark Connector does not expose a custom resolver interface. It accepts a fixed set of driver options (connection timeouts, encryption, authentication) with no arbitrary config passthrough. It has a built-in multi-URL feature where comma-separated URLs in the `url` option are parsed into a static resolver, but this resolver follows the same Java driver limitation: initial address only, not routing table addresses.

Databricks serverless compute supports Python and SQL notebooks only. No Scala, no Java, no JAR installation, no Py4J access to the JVM. A Spark Java prototype is not possible on serverless. The Python driver can be installed via `%pip install neo4j` and tested directly.

### Revised Assessment

**Option 1 (NCC Wildcard Domain Rule):** Not viable. NCC does not support wildcards. Exact FQDN matching only.

**Option 2 (Multiple NCC Domain Names in one PE rule):** The clear frontrunner. Four domains fit in one rule. The PATCH API enables updates without NCC detachment. If the routing table hostnames are added to the PE rule, NCC will intercept DNS for all of them and route traffic through the private endpoint. HAProxy in TCP passthrough mode preserves the original SNI, so Aura's edge sees the correct member hostname. This is an NCC configuration change with no infrastructure modifications.

**Option 3 (Neo4j Driver Custom Resolver):** Insufficient as a standalone solution. The resolver does not intercept reader/writer connections in either driver implementation. Even if routing table refresh works through the resolver, actual query traffic to cluster members bypasses it. On serverless, only the Python driver is available (no Java/Scala). The Python resolver covers routing table router contacts but not query connections. This option cannot replace NCC domain rules; at best it complements them.


## Prototype Plan

### Which Infrastructure to Use

Either project works for testing NCC behavior because NCC sits in front of both architectures identically. The NCC private endpoint rule, domain matching, and DNS interception are the same regardless of whether the backend is an ILB with HAProxy or an Application Gateway.

The App Gateway is currently deployed and working. Use it for the initial NCC domain prototype to avoid redeployment time. If the NCC domain approach validates successfully, repeat the test on the LB project before making a customer recommendation, since the LB architecture is the defensible long-term path.


### Prototype 1: Multiple NCC Domain Names (Primary)

This is the highest-value test. If it works, `neo4j+s://` is enabled with zero infrastructure changes.

**Step 0: Verify TLS certificates cover the routing table hostnames.**

Before touching NCC configuration, confirm that Aura's TLS certificate covers the routing table hostnames. These are hostnames Aura assigns and returns in the routing table, so the certificate should cover them, but verification eliminates a variable before the tunnel test.

Run `openssl s_client` against Aura's public IP using each routing hostname as the SNI:

```bash
openssl s_client -connect 20.127.122.152:7687 -servername p-a5e20181-83e0-0001.production-orch-1275.neo4j.io
openssl s_client -connect 20.127.122.152:7687 -servername p-a5e20181-83e0-0002.production-orch-1275.neo4j.io
openssl s_client -connect 20.127.122.152:7687 -servername p-a5e20181-83e0-0003.production-orch-1275.neo4j.io
```

If the certificate validates (look for `Verify return code: 0 (ok)` and check that the subject or SAN covers the hostname), TLS SNI is confirmed to work. If it fails, the routing table hostnames may not be independently addressable via SNI, and the prototype cannot succeed.

This also confirms that the NCC domain approach solves the TLS SNI problem. NCC only intercepts DNS resolution, not the TLS handshake. The driver sends the hostname it is connecting to as the SNI in the TLS ClientHello. Both the App Gateway L4 TCP listener and the LB project's HAProxy operate in TCP passthrough mode, forwarding raw bytes without terminating or inspecting TLS. The SNI reaches Aura untouched. Aura receives the original member hostname as SNI and routes to the correct cluster member. The certificate covers the hostname because Aura is the TLS endpoint and these are hostnames Aura assigned. The full chain works: DNS resolution through NCC, TLS SNI preserved through the TCP passthrough layer (App Gateway or HAProxy), certificate valid at Aura's edge.

**Step 1: Collect the routing table hostnames.**

Run `routing_poc/inspect_routing_table.py` from the app-gateway-pl project. The App Gateway instance returns:

```
p-a5e20181-83e0-0001.production-orch-1275.neo4j.io
p-a5e20181-83e0-0002.production-orch-1275.neo4j.io
p-a5e20181-83e0-0003.production-orch-1275.neo4j.io
```

Plus the connection FQDN: `a5e20181.databases.neo4j.io`. Four domains total, within the 10-domain limit per PE rule.

**Step 2: Update the NCC PE rule with all four domains.**

The `deploy.py` script has an `update-pe-domains` command that automates this. It connects to Aura BC, fetches the routing table, finds the existing PE rule, and PATCHes it with all four domains:

```bash
uv run python deploy.py update-pe-domains --profile <databricks-cli-profile>
```

Under the hood, this calls the Databricks REST API:

```
PATCH /api/2.0/accounts/{ACCOUNT_ID}/network-connectivity-configs/{NCC_ID}/private-endpoint-rules/{RULE_ID}?update_mask=domain_names
```

The NCC does not need to be detached from the workspace.

**Step 3: Wait for propagation and restart serverless compute.**

The documentation says changes take approximately five minutes to propagate. Any running serverless compute should be restarted to pick up the new DNS routing.

**Step 4: Test neo4j+s:// from a Databricks serverless notebook.**

Create a notebook cell that connects with `neo4j+s://` instead of `bolt+s://`:

```python
%pip install neo4j

from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    "neo4j+s://<connection-fqdn>",
    auth=("neo4j", dbutils.secrets.get(scope="...", key="password")),
    max_connection_lifetime=240,
    liveness_check_timeout=120,
)

# Force routing table population
records, summary, keys = driver.execute_query("RETURN 1 AS n")
print(f"Server: {summary.server.address}")

# Inspect the routing table
pool = driver._pool
if hasattr(pool, "routing_tables"):
    for db, table in pool.routing_tables.items():
        print(f"Database: {db}, TTL: {table.ttl}s")
        for role in ("routers", "readers", "writers"):
            addrs = getattr(table, role, [])
            print(f"  {role}: {[f'{a[0]}:{a[1]}' for a in addrs]}")

# Test read distribution — run multiple queries and check which server handles them
for i in range(5):
    records, summary, keys = driver.execute_query(
        "RETURN 1 AS n",
        routing_="r",  # route to a reader
    )
    print(f"Query {i}: server={summary.server.address}")

driver.close()
```

**What success looks like:** The driver connects, populates the routing table, and subsequent queries with `routing_="r"` land on different cluster members. The `summary.server.address` values should show traffic distributed across the routing table entries.

**What failure looks like:** The driver connects on the initial FQDN but fails when attempting connections to routing table hostnames. This would indicate NCC is not matching the additional domains, or the TLS certificate does not cover the routing hostnames, or Aura's edge is rejecting the connection for another reason. The error message will narrow it down.

**Step 5: Validate hostname stability.**

If Step 4 succeeds, leave the configuration in place for several days and re-run the routing table inspection periodically. Confirm that the three member hostnames remain stable. If they change during maintenance or scaling events, the PATCH API update would need to be automated, which is feasible but adds operational complexity.


### Prototype 1 Results (Validated 2026-03-20)

Step 0 passed. Aura uses a wildcard certificate `CN=*.production-orch-1275.neo4j.io` (Let's Encrypt, Verify return code: 0). All three routing table hostnames match.

Steps 1-3 completed. The `deploy.py update-pe-domains` command fetched the routing table, found three member hostnames, and PATCHed the PE rule from 1 domain to 4 domains. NCC status confirmed ESTABLISHED with all four domains.

Step 4 passed. A Databricks serverless notebook connected with `neo4j+s://` through the App Gateway Private Link tunnel. The driver fetched the routing table (3 routers, 2 readers, 1 writer), connected to member hostnames through the private endpoint, and executed queries. TLS SNI was preserved end-to-end through the App Gateway L4 TCP passthrough.

The NCC multi-domain approach solves both the TLS SNI problem and the routing table hostname resolution problem with a single API call and zero infrastructure changes. The App Gateway architecture is the preferred path because it eliminates the reverse proxy entirely: no VMs, no HAProxy, no NAT Gateway.


### Prototype 2: Python Driver Resolver on Serverless — Will Not Test

The NCC multi-domain approach validated in Prototype 1 solves DNS interception at the network layer, covering all connections regardless of driver behavior. The driver resolver is unnecessary.

Research confirmed the resolver is insufficient as a standalone solution: the Java driver resolver does not intercept routing table addresses, the Python driver resolver does not intercept reader/writer query connections, and the Spark connector exposes no custom resolver interface.


### Prototype 3: Spark Connector with neo4j+s:// — Will Not Test

The NCC multi-domain approach works at the DNS layer, which means it applies to any driver implementation (Python, Java, Spark connector) without driver-specific configuration. The Spark connector test was contingent on Prototype 1 succeeding. Since it succeeded, the Spark connector should work identically because NCC intercepts DNS for all four domains before the driver is involved. This can be confirmed in a production integration test rather than a separate prototype.
