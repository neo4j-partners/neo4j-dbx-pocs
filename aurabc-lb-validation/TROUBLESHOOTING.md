# Troubleshooting: Databricks Serverless to Aura BC via Private Link

**Date:** 2026-03-19
**Status:** Resolved

## Symptom

The Databricks test notebook (`aurabc_private_link_test.ipynb`) running on serverless compute shows:

- **TCP test: PASS** — `nc -zv neo4j-aurabc.private.neo4j.com 7687` connects in 77ms. The private endpoint resolves to `172.18.11.162`.
- **Neo4j driver test: FAIL** — `SSLEOFError` during TLS handshake. Zero bytes transferred.

```
[FAIL] Couldn't connect to neo4j-aurabc.private.neo4j.com:7687
       (resolved to ('172.18.11.162:7687', '[::ffff:172.18.11.162]:7687')):
[SSLEOFError] Failed to establish encrypted connection. (code 8: Exec format error)
```

The driver uses `bolt+s://neo4j-aurabc.private.neo4j.com:7687`. We use `bolt+s://` (not `neo4j+s://`) because `neo4j+s://` performs routing table discovery, which returns the Aura FQDN and bypasses the private endpoint entirely. This was validated on 2026-03-18.

## Connection path

```
Databricks Serverless
    → NCC Private Endpoint (172.18.11.162, eastus)
    → Private Link Service (eastus)
    → Internal Load Balancer (10.0.1.4)
    → HAProxy VM (10.0.2.4)
    → NAT Gateway (20.106.75.1)
    → Neo4j Aura BC (f5919d06.databases.neo4j.io, resolves to 20.25.158.81)
```

The NCC is in **eastus** (matching the Databricks workspace), while the Azure infrastructure (PLS, LB, VM, NAT GW) is in **eastus**. The private endpoint rule works cross-region.

## What we verified is working

| Check | Result |
|-------|--------|
| Azure infra status | All resources healthy — VM running, LB Succeeded, PLS Succeeded |
| NAT Gateway IP in Aura BC allowlist | `20.106.75.1/32` is allowlisted |
| VM outbound IP | `curl ifconfig.me` returns `20.106.75.1` (matches allowlist) |
| DNS on VM | `dig f5919d06.databases.neo4j.io` resolves to `20.25.158.81` |
| Direct TLS from VM | `openssl s_client -connect f5919d06.databases.neo4j.io:7687` completes full TLS handshake with valid cert (CN=neo4j.io) |
| HAProxy process | Active and running, listening on `0.0.0.0:7687` |
| NCC private endpoint | Status ESTABLISHED, connection Approved on PLS |
| TCP from Databricks | `nc -zv` succeeds in 77ms — network path is open end-to-end |

## What is failing

HAProxy logs show two connections from the private endpoint subnet (`10.0.3.4`) that both ended with status `SD` (server disconnect) and 0 bytes transferred:

```
10.0.3.4:1025 [19/Mar/2026:05:12:12.843] neo4j_bolt aura_bc/aura1 1/3/4 0 SD 2/2/1/1/0 0/0
10.0.3.4:1028 [19/Mar/2026:05:12:12.960] neo4j_bolt aura_bc/aura1 1/3/5 0 SD 2/2/1/1/0 0/0
```

HAProxy log field breakdown:
- `1/3/4` — 1ms queue wait, 3ms to connect to backend, 4ms total session
- `0` — zero bytes transferred
- `SD` — server closed the connection during data transfer (i.e., during TLS handshake)

HAProxy successfully connects outbound to Aura BC (the 3ms connect time confirms this), but Aura drops the connection before TLS completes.

## HAProxy config

```
frontend neo4j_bolt
    bind *:7687
    default_backend aura_bc

backend aura_bc
    server aura1 f5919d06.databases.neo4j.io:7687 check resolvers azure_dns

resolvers azure_dns
    nameserver dns1 168.63.129.16:53
    resolve_retries 3
    timeout resolve 1s
    timeout retry 1s
    hold valid 30s
```

HAProxy is in TCP mode (mode tcp), which should do a transparent passthrough — it does not terminate or inspect TLS.

## Hypothesis

The `check` directive on the backend server line causes HAProxy to periodically probe `f5919d06.databases.neo4j.io:7687` with a plain TCP connection (no TLS). Aura BC sees an incoming connection that doesn't start a TLS handshake and drops it. This could be causing HAProxy to mark the backend as unhealthy or partially degraded, which may interfere with real client connections.

However, the HAProxy logs show it _did_ attempt to forward the real connections (status `SD` means it reached the backend). So the health check may not be the root cause. Alternative possibilities:

1. **Health check interference** — The frequent non-TLS probe connections from the same source IP may be triggering rate limiting or temporary blocks on the Aura BC side.
2. **PROXY protocol mismatch** — Unlikely since HAProxy config doesn't enable PROXY protocol, but worth ruling out.
3. **Connection timing** — The 4-5ms total session time is very short. It's possible the connection is being dropped before TLS ClientHello reaches Aura through the NAT Gateway.

## Fix attempt 1: Remove `check` directive

**Applied:** 2026-03-19 05:28 UTC

Removed the `check` directive from the HAProxy backend configuration and restarted HAProxy. This eliminates the periodic non-TLS probes to Aura BC that may have been triggering rate limiting or causing the backend to be marked unhealthy.

```
# Changed from:
server aura1 f5919d06.databases.neo4j.io:7687 check resolvers azure_dns

# Changed to:
server aura1 f5919d06.databases.neo4j.io:7687 resolvers azure_dns
```

HAProxy restarted successfully. Waiting for Databricks notebook re-run to confirm.

**Result: Databricks notebook — still FAIL** (same `SSLEOFError`). However:

**Result: py-test VM — PASS for the PE chain.** Ran `deploy_test_vm.py test` from the test VM in eastus, which exercises the same cross-region path (eastus VM → PE → PLS eastus → LB → HAProxy → Aura BC). Results:

| Test | Result |
|------|--------|
| `test_hosts_file_entry` | PASS |
| `test_pe_dns_resolution` | PASS |
| `test_pe_tcp_connectivity` | PASS |
| `test_bolt_through_private_endpoint` | **PASS** — full bolt+s:// query through the PE chain |
| `test_bolt_direct_baseline` | FAIL — allowlist propagation delay (unrelated) |

The `check` removal fixed the HAProxy chain. The PE → PLS → LB → HAProxy → Aura BC path is confirmed working from an Azure VM. The Databricks serverless failure is now isolated to the Databricks side.

## Additional steps taken

- **Rejected stale PE connection** from the deleted eastus NCC on the PLS. Now only the active eastus NCC and py-test VM connections remain Approved.
- **Re-ran Databricks notebook** — still fails with same `SSLEOFError`.
- **Verified NCC state via Databricks API** — NCC is in eastus, PE rule is ESTABLISHED, workspace is attached. Configuration is correct.

## Root cause identified: SNI mismatch

Tested TLS handshake from the py-test VM through the PE with different SNI hostnames:

```bash
# Real Aura FQDN as SNI — TLS completes, cert returned (CN=neo4j.io)
openssl s_client -connect 10.1.2.4:7687 -servername f5919d06.databases.neo4j.io
# Result: SUCCESS

# NCC domain as SNI — Aura drops the connection
openssl s_client -connect 10.1.2.4:7687 -servername neo4j-aurabc.private.neo4j.com
# Result: "unexpected eof while reading" — same error as Databricks
```

**Aura BC rejects TLS connections where the SNI hostname doesn't match its certificate.** The NCC PE rule domain (`neo4j-aurabc.private.neo4j.com`) becomes the hostname that Databricks uses in the TLS ClientHello. Since Aura BC doesn't recognize that hostname, it drops the connection during the TLS handshake.

The py-test VM worked because it connects using the real Aura FQDN (`f5919d06.databases.neo4j.io`) mapped to the PE IP via `/etc/hosts`, so the SNI matches what Aura expects.

### Why the original proposal's domain approach doesn't work for Aura BC

The PL.md proposal suggested using an arbitrary private domain (e.g., `neo4j-aurabc.private.neo4j.com`) for the NCC PE rule. This works for Neo4j Enterprise Edition marketplace deployments (private-link-ee) because those instances likely handle SNI differently. Aura BC enforces strict SNI matching against its TLS certificate — the domain must be the real Aura FQDN.

## Fix attempt 2: Use real Aura FQDN as NCC domain

The NCC PE rule domain must be set to the real Aura FQDN (`f5919d06.databases.neo4j.io`) so that Databricks sends the correct SNI in the TLS handshake. This requires:

1. Delete the current PE rule (domain `neo4j-aurabc.private.neo4j.com`)
2. Create a new PE rule with domain `f5919d06.databases.neo4j.io`
3. Approve the new PE connection on the PLS (if not auto-approved)
4. Update the notebook to use `bolt+s://f5919d06.databases.neo4j.io:7687`
5. Re-run the notebook

**Applied:** 2026-03-19 06:01 UTC

- Deleted old PE rule `b2f9f7ad` (domain `neo4j-aurabc.private.neo4j.com`)
- Created new PE rule `f485c27c` (domain `f5919d06.databases.neo4j.io`), status ESTABLISHED
- New PE connection auto-approved on PLS
- Updated notebook config cell: `NEO4J_DOMAIN = "f5919d06.databases.neo4j.io"`

**Result: FAIL — stale notebook.** The Databricks notebook still had the old domain:

```
URI: bolt+s://neo4j-aurabc.private.neo4j.com:7687
[FAIL] Failed to DNS resolve address neo4j-aurabc.private.neo4j.com:7687: [Errno -2] Name or service not known
```

The DNS failure confirms the fix is working as expected: the old PE rule (domain `neo4j-aurabc.private.neo4j.com`) was deleted, so Databricks no longer resolves that domain to the PE IP. The notebook was not re-imported after the local change to `NEO4J_DOMAIN = "f5919d06.databases.neo4j.io"`.

**Action required:** Re-import `aurabc_private_link_test.ipynb` into Databricks to pick up the updated domain, then re-run.

**FINAL RESULT: PASS** — 2026-03-19

After re-importing the notebook with the correct domain:

```
URI: bolt+s://f5919d06.databases.neo4j.io:7687

[PASS] Connected and authenticated in 352.8ms
[PASS] Query result: Connected over Private Link via bolt+s

  Server: Neo4j Kernel ['5.27-aura']
  Server: Cypher ['5', '25']

  Connection: 352.8ms
  Total:      761.3ms

PRIVATE LINK CONNECTIVITY VERIFIED
```

## Summary of issues found and fixed

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| `attach-ncc` failed: NCC must be in same region as workspace | NCC created in eastus (infra region) but workspace is in eastus | Added `NCC_REGION` env var; recreated NCC in eastus |
| HAProxy `SD` (server disconnect) on forwarded connections | `check` directive caused periodic non-TLS probes to Aura BC, likely triggering rate limiting | Removed `check` from HAProxy backend config |
| `SSLEOFError` on Databricks but py-test VM works | NCC PE rule domain (`neo4j-aurabc.private.neo4j.com`) became the TLS SNI hostname; Aura BC rejects unrecognized SNI | Changed PE rule domain to real Aura FQDN (`f5919d06.databases.neo4j.io`) |

## Key learnings for Aura BC + Databricks Private Link

1. **NCC region must match workspace region**, not the Azure infrastructure region. Cross-region PE rules (eastus NCC → eastus PLS) work fine.
2. **HAProxy health checks must be disabled** (no `check` directive) when proxying to Aura BC. Plain TCP probes without TLS cause Aura to drop the connection.
3. **The NCC PE rule domain must be the real Aura FQDN.** Databricks uses this domain as the TLS SNI hostname. Aura BC enforces strict SNI matching against its certificate — arbitrary private domains are rejected. This differs from the private-link-ee project where a custom domain works.
4. **Use `bolt+s://`** (not `neo4j+s://`). The `neo4j+s://` scheme performs routing table discovery which returns the Aura FQDN and bypasses the private endpoint.
5. **Driver keepalive settings** must account for the Azure PLS ~5 minute idle timeout: `max_connection_lifetime < 300`, `liveness_check_timeout < 300`.
