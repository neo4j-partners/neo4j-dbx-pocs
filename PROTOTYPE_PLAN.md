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


### Prototype 2: Python Driver Resolver on Serverless (Supplementary)

This test is worth running even though the resolver alone is insufficient. It establishes whether the Python driver's partial resolver coverage produces useful behavior on serverless, and it provides a fallback data point if the NCC domain approach has unexpected issues.

**Step 1: Create a serverless notebook with a custom resolver.**

```python
%pip install neo4j

import neo4j

CONNECTION_FQDN = "<connection-fqdn>"

ROUTING_MEMBERS = [
    "p-8cc8f63c-365a-0001.production-orch-1275.neo4j.io",
    "p-8cc8f63c-365a-0002.production-orch-1275.neo4j.io",
    "p-8cc8f63c-365a-0003.production-orch-1275.neo4j.io",
]

def tunnel_resolver(address):
    """Resolve all routing table hostnames to the connection FQDN."""
    host = address[0]
    if host in ROUTING_MEMBERS or host == CONNECTION_FQDN:
        yield neo4j.Address((CONNECTION_FQDN, 7687))
    else:
        yield address

driver = neo4j.GraphDatabase.driver(
    f"neo4j+s://{CONNECTION_FQDN}",
    auth=("neo4j", dbutils.secrets.get(scope="...", key="password")),
    resolver=tunnel_resolver,
    max_connection_lifetime=240,
    liveness_check_timeout=120,
)

records, summary, keys = driver.execute_query("RETURN 1 AS n")
print(f"Connected: {summary.server.address}")

driver.close()
```

**What this tests:** Whether the resolver intercepts the routing table router addresses during table refresh on serverless. Based on Python driver source code analysis, it should. But the resolver will not intercept reader/writer connections for actual queries.

**What success looks like:** The driver connects and can refresh the routing table without errors. Queries execute successfully. However, traffic may still route to the public Aura endpoint for reader/writer connections rather than through the PE tunnel.

**What failure looks like:** The driver errors on routing table refresh because it cannot reach the routers, or because the TLS SNI from the resolver-rewritten address does not match what Aura expects. If the resolver maps all addresses to the connection FQDN, the SNI will be the connection FQDN for all connections, and Aura may or may not route to the correct member.

**Important caveat:** Even if this test "succeeds" (queries return results), the traffic for read/write queries may be flowing over the public internet rather than through the PE tunnel. The resolver does not control the path for those connections. To verify the actual network path, the Aura BC IP allowlist would need to be locked down to only the NAT Gateway IP (or App Gateway IP). If queries still succeed with only the tunnel IP allowlisted, traffic is flowing through the tunnel. If they fail, the reader/writer connections are bypassing the tunnel.

This is why Option 2 (NCC domain rules) is the primary approach. It solves DNS interception at the network layer, covering all connections regardless of driver behavior.


### Prototype 3: Spark Connector with neo4j+s:// (If Option 2 Succeeds)

If the NCC domain rule prototype validates, the next step is confirming that the Spark connector also works with `neo4j+s://` through the tunnel. The Spark connector uses the Java driver internally, so its routing behavior may differ from the Python driver test.

**Step 1: Install the Spark connector on a classic compute cluster.**

The Spark connector cannot be installed on serverless. Use a classic cluster with the Maven coordinates:

```
org.neo4j:neo4j-connector-apache-spark_2.12:5.4.0_for_spark_3
```

**Step 2: Read data using neo4j+s:// scheme.**

```python
df = (spark.read
    .format("org.neo4j.spark.DataSource")
    .option("url", "neo4j+s://<connection-fqdn>:7687")
    .option("authentication.basic.username", "neo4j")
    .option("authentication.basic.password", dbutils.secrets.get(scope="...", key="password"))
    .option("connection.max.lifetime.msecs", "240000")
    .option("connection.liveness.timeout.msecs", "120000")
    .option("query", "RETURN 1 AS n")
    .load())

df.show()
```

**What this tests:** Whether the Spark connector's internal Java driver can connect with `neo4j+s://` and route queries through the tunnel when NCC domain rules cover the routing table hostnames. The Java driver resolver does not intercept routing table addresses, but it does not need to if NCC handles DNS interception at the network level.

**What success looks like:** The DataFrame loads successfully. The Java driver fetches the routing table, resolves member hostnames through NCC (which intercepts DNS for all four domains), and connects through the PE tunnel. Queries execute against cluster members.

**Limitation:** This test requires a classic compute cluster, not serverless. NCC PE rules apply to both classic and serverless, but the NCC must be attached to the workspace and the cluster must be in the same region. Classic clusters with NCC may behave differently from serverless in terms of DNS interception. If this is a concern, the test can be deferred until the serverless Python driver test (Prototype 1) validates the NCC approach.

**Alternative for serverless:** Use the Spark connector's built-in multi-URL feature to specify all routing members explicitly:

```python
url = "neo4j+s://<connection-fqdn>:7687,neo4j+s://p-8cc8f63c-365a-0001.production-orch-1275.neo4j.io:7687,neo4j+s://p-8cc8f63c-365a-0002.production-orch-1275.neo4j.io:7687,neo4j+s://p-8cc8f63c-365a-0003.production-orch-1275.neo4j.io:7687"
```

This passes the member addresses as static resolvers via the connector's comma-separated URL parsing. The Java driver would use these for initial connection bootstrapping. Combined with NCC domain rules handling DNS for all hostnames, this may provide additional resilience.


## Execution Order

1. **Run Prototype 1 (NCC domain rules) first.** It requires no code, no infrastructure changes, and no new dependencies. It is a PATCH API call, a five-minute wait, and a notebook test. If it works, it answers the primary question: can `neo4j+s://` work through the existing private link tunnel by adding routing table hostnames to the NCC PE rule?

2. **Run Prototype 2 (Python resolver) in parallel if desired.** It is a notebook-only test that can run alongside Prototype 1. It provides data on driver resolver behavior on serverless regardless of the NCC outcome.

3. **Run Prototype 3 (Spark connector) only if Prototype 1 succeeds.** There is no point testing the Spark connector until the NCC domain approach is validated. The Spark connector test confirms that the Java driver path also works, which matters for production workloads that use Spark DataFrames rather than raw driver calls.

Use the App Gateway infrastructure for all three prototypes since it is currently deployed. If the NCC domain approach validates, repeat the Prototype 1 test on the LB project to confirm identical behavior before making a customer recommendation.
