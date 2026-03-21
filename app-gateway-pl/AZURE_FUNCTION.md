# Operational Monitoring for the App Gateway Private Link Architecture

## The Architecture Works, but Depends on State It Doesn't Control

The Application Gateway Private Link architecture connecting Databricks serverless to Neo4j Aura BC has been validated end-to-end. Both `bolt+s://` (direct connections) and `neo4j+s://` (client-side routing with read/write splitting) work through the tunnel. The phased deployment is a one-time setup, and the NCC multi-domain PE rule enables full protocol support with zero ongoing infrastructure changes.

But "validated" describes a moment in time. The architecture depends on three pieces of external state: the hostnames in Aura BC's routing table, the connection state of the NCC private endpoint, and the TLS certificate that Aura serves on its shared ingress. None of these are under our control. Any of them can change without warning, and when they do, the failure mode is silent. Connections time out or get rejected. No alarm fires. The Databricks notebook that worked yesterday returns a cryptic driver error today, and the root cause sits in a hostname that Aura reassigned during overnight maintenance.

The correct response is a lightweight monitoring function that checks each of these external dependencies on a schedule, reconciles drift automatically where possible, and alerts when it can't.


## What Can Break

Three categories of failure apply to this architecture. A fourth, the Private Link idle timeout, is often misunderstood and warrants clarification.

### Routing Table Hostname Drift

The `neo4j+s://` protocol works because the NCC PE rule contains four domains: the connection FQDN (`*.databases.neo4j.io`) and three routing table member hostnames (`p-*.production-orch-*.neo4j.io`). NCC intercepts DNS for all four and routes the connections through the private endpoint.

These routing table hostnames are assigned by Aura. They encode instance and member identifiers that appear stable during normal operation, but Neo4j provides no contractual guarantee of stability. Cluster scaling, maintenance events, or failover could reassign them. If a hostname changes and the PE rule still lists the old one, NCC stops intercepting DNS for the new hostname. The driver resolves it via public DNS, connects directly to Aura's public IP, and Aura rejects the connection because the source IP isn't allowlisted.

The `bolt+s://` protocol would continue working in this scenario because it only uses the connection FQDN. The failure is specific to `neo4j+s://` and its routing table connections.

This is the highest-priority monitoring concern. The fix is a single Databricks API call (PATCH the PE rule's `domain_names`), and the logic already exists in `deploy.py update-pe-domains`. An automated function can detect and remediate the drift within minutes.

### NCC Private Endpoint Connection State

The NCC private endpoint connection has a lifecycle: PENDING, ESTABLISHED, DISCONNECTED, REJECTED. Normal operation requires ESTABLISHED. Azure platform issues, resource modifications, or subscription-level changes can move the connection to DISCONNECTED or REJECTED without any action on our part.

When the connection leaves ESTABLISHED, all traffic through the tunnel stops. Both `bolt+s://` and `neo4j+s://` fail completely. The Databricks workspace can still resolve hostnames via NCC, but the private endpoint behind those resolutions is no longer forwarding packets.

The current way to detect this is running `deploy.py ncc-status` manually. The function should check this on every run and alert immediately if the state changes, because the remediation (re-creating the PE rule and re-approving) requires manual intervention.

### TLS Certificate Changes

Aura BC serves a wildcard Let's Encrypt certificate (`*.production-orch-*.neo4j.io`) that covers all routing table member hostnames. The App Gateway operates at Layer 4 and passes TLS through without termination, so the driver negotiates TLS directly with Aura. If the certificate expires, changes its SAN pattern, or switches to a different issuing authority, TLS handshakes fail for every connection through the tunnel.

Let's Encrypt certificates have a 90-day expiry and auto-renew. Under normal circumstances this requires no attention. The monitoring concern is the edge case: a renewal failure, a domain migration, or a change in the SAN pattern that no longer covers the routing table hostnames. Checking the certificate's expiry date and SAN coverage once a day catches these issues before they cause production failures.

### Private Link Idle Timeout (Not a Monitoring Concern)

The Private Link tunnel has an approximately 300-second idle timeout on individual TCP connections. This is often misunderstood as a risk to the tunnel itself. It is not.

The tunnel is a resource that exists as long as the PE connection state is ESTABLISHED. It doesn't degrade from sitting unused. A Databricks notebook can sit idle for hours, and when a new job starts, the driver opens fresh connections through the tunnel and those connections work immediately. The tunnel doesn't need keepalive traffic to stay alive.

The timeout applies to a single TCP connection that has been opened and is sitting idle with no data flowing. If a Spark job opens a connection to Neo4j, reads a batch of data, then spends six minutes processing that batch without sending or receiving anything on the Neo4j connection, the Private Link infrastructure drops the idle TCP session. When the job tries to use that connection again, it's dead.

The mitigation is driver-level configuration, not infrastructure monitoring. Setting `max_connection_lifetime=240` forces the driver to cycle connections before they hit the 300-second threshold. Setting `liveness_check_timeout=120` causes the driver to verify connections are alive before using them. These settings are already configured in the test notebook and documented in the project.

A monitoring function cannot prevent idle timeouts because it has no access to the connection pools inside a running Databricks job. The driver settings are the correct and complete solution. No monitoring action is needed for this risk.


## The Proposed Function

An Azure Function running on a Timer trigger performs three checks on a configurable schedule. The function is a single Python file that reuses the same Aura and Databricks API patterns already proven in `deploy.py`. It runs in the same Azure subscription as the App Gateway, using a Consumption plan that costs effectively nothing at the frequency required.

### Check 1: Routing Table Sync

The function connects to Aura BC with `neo4j+s://`, fetches the routing table, and compares the member hostnames against the current NCC PE rule's `domain_names` array.

If the hostnames match, no action is taken. If they differ, the function PATCHes the PE rule with the updated domain list. This is the same operation that `deploy.py update-pe-domains` performs manually. After updating, the function logs the change and sends an alert with the old and new hostname lists.

A daily schedule is sufficient for baseline monitoring. If hostname stability proves to be a concern after collecting data with `inspect_routing_table.py`, the frequency can be increased.

### Check 2: NCC PE Connection State

The function calls the Databricks Account API to retrieve the NCC configuration and inspects the `connection_state` field of the PE rule. If the state is anything other than ESTABLISHED, the function sends an alert.

Automatic remediation for connection state changes is possible but risky. Re-creating a PE rule and re-approving it involves multiple API calls across Azure and Databricks, and a failure partway through could leave the NCC in a worse state. The function should alert and leave remediation to an operator.

### Check 3: TLS Certificate Validation

The function opens a TLS connection to Aura's public endpoint using the connection FQDN as SNI, retrieves the server certificate, and checks two things: that the certificate expires more than 14 days from now, and that the Subject Alternative Name (SAN) field still contains the wildcard pattern covering the routing table hostnames.

If either check fails, the function sends an alert. There is no automatic remediation because certificate issues are on Aura's side.

### Alerting

The function needs a single alert channel. Options ranked by simplicity:

An Azure Monitor alert rule tied to the function's Application Insights logs is the lowest-effort option. The function writes structured log entries, and an alert rule fires when it sees a log entry with a severity of "warning" or above. This requires no additional infrastructure.

A Teams or Slack webhook is more visible. The function sends an HTTP POST with a summary message when any check detects a change or failure. One environment variable for the webhook URL.

Email via Azure Communication Services or SendGrid works if the audience doesn't use Teams or Slack.

### Credentials

The function needs three sets of credentials:

Neo4j Aura BC credentials (username, password, URI) for routing table inspection. These are the same values stored in the `.env` file and in the Databricks secret scope.

A Databricks account-level API token for reading NCC state and PATCHing PE rules. This is the same token used by `deploy.py` commands that accept `--profile`.

No Azure credentials are needed for the TLS certificate check because it connects to Aura's public endpoint directly. The function's managed identity handles Azure resource access if needed for logging.

All credentials should be stored in Azure Key Vault and referenced via the function's application settings. The Consumption plan supports Key Vault references natively.


## What This Does and Does Not Replace

The Azure Function replaces the need to manually run `deploy.py update-pe-domains` and `deploy.py ncc-status` on a schedule. It automates the hostname sync that `inspect_routing_table.py` currently tracks for manual review.

It does not replace the deployment scripts (`setup_azure.py`, `deploy.py`). Initial setup, phased deployment, NCC creation, and teardown remain manual operations. The function monitors steady-state operation, not deployment lifecycle.

It does not address the Private Link idle timeout. That is a driver configuration concern handled by `max_connection_lifetime` and `liveness_check_timeout` settings in the application code.


## Tradeoffs

The Consumption plan introduces cold-start latency (a few seconds on the first invocation after idle), but this is irrelevant for a scheduled monitoring function that doesn't serve user requests.

The function stores Aura BC credentials outside the Databricks workspace. This expands the credential surface. Key Vault with access policies and managed identity mitigates this, but it's an additional secret location to manage.

The routing table sync is self-healing for hostname drift, which means it can mask an underlying problem. If Aura starts reassigning hostnames frequently, the function silently fixes it each time. The alert on each PATCH ensures visibility, but the operator needs to watch for patterns rather than individual events. The history file from `inspect_routing_table.py` provides the longitudinal view that the function's point-in-time alerts cannot.

If hostname instability proves rare (as current evidence suggests), the function's primary value shifts from remediation to peace of mind: confirmation that the architecture's external dependencies haven't changed since the last check.
