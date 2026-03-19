"""
End-to-end tests for the Aura BC Application Gateway + Private Link setup.

Runs on the test VM deployed by deploy_test_vm.py. Traffic path:
  Test VM (eastus) → PE → App Gateway (westus3) → Aura BC

Tests both bolt+s:// and neo4j+s:// to determine which connection schemes
work through the Application Gateway TCP proxy.

Usage (from the VM):
    uv run pytest -v -s
"""

import socket

from neo4j import GraphDatabase


# ---------------------------------------------------------------------------
# Infrastructure validation
# ---------------------------------------------------------------------------

def test_hosts_file_entry(neo4j_config):
    """Verify cloud-init added the Aura FQDN → PE IP entry in /etc/hosts."""
    hostname = neo4j_config["hostname"]
    with open("/etc/hosts") as f:
        content = f.read()
    assert hostname in content, (
        f"{hostname} not found in /etc/hosts. "
        f"Was the VM deployed with the correct auraFqdn parameter?"
    )


def test_pe_dns_resolution(neo4j_config, pe_ip):
    """Verify the Aura FQDN resolves to the PE private IP via /etc/hosts."""
    hostname = neo4j_config["hostname"]
    resolved = socket.gethostbyname(hostname)
    assert resolved == pe_ip, (
        f"{hostname} resolved to {resolved}, expected PE IP {pe_ip}"
    )


def test_pe_tcp_connectivity(neo4j_config):
    """TCP connectivity to the PE IP on the bolt port."""
    hostname = neo4j_config["hostname"]
    resolved = socket.gethostbyname(hostname)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        sock.connect((resolved, 7687))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# bolt+s:// tests (known-working scheme through intermediaries)
# ---------------------------------------------------------------------------

def test_bolt_through_private_endpoint(neo4j_config, appgw_ip):
    """Full bolt+s:// connection through PE → App Gateway → Aura BC.

    /etc/hosts maps the Aura FQDN to the PE IP, so the driver connects through
    the private endpoint while TLS verification passes (cert is *.databases.neo4j.io).
    """
    hostname = neo4j_config["hostname"]

    resolved = socket.gethostbyname(hostname)
    assert resolved.startswith("10."), (
        f"Expected {hostname} to resolve to PE IP (10.x), got {resolved}"
    )

    driver = GraphDatabase.driver(
        f"bolt+s://{hostname}",
        auth=(neo4j_config["username"], neo4j_config["password"]),
        max_connection_lifetime=240,
        liveness_check_timeout=120,
        connection_acquisition_timeout=30,
    )
    try:
        records, summary, _ = driver.execute_query("RETURN 1 AS n")
        assert records[0]["n"] == 1
        print(f"\n  bolt+s:// via PE ({resolved}) → App GW ({appgw_ip}) → Aura BC")
        print(f"  Server: {summary.server.address}")
    finally:
        driver.close()


# ---------------------------------------------------------------------------
# neo4j+s:// tests (routing protocol — the key experiment)
# ---------------------------------------------------------------------------

def test_neo4j_routing_through_private_endpoint(neo4j_config, appgw_ip):
    """neo4j+s:// connection through PE → App Gateway → Aura BC.

    This is the core experiment. neo4j+s:// triggers routing table discovery,
    which causes the driver to open connections to backend hostnames returned
    in the routing table. The question is whether those connections also route
    through the App Gateway (via /etc/hosts → PE IP) or bypass it.

    If this test passes, the full routing protocol works through the gateway,
    restoring client-side routing, failover discovery, and read/write splitting.
    """
    hostname = neo4j_config["hostname"]

    resolved = socket.gethostbyname(hostname)
    assert resolved.startswith("10."), (
        f"Expected {hostname} to resolve to PE IP (10.x), got {resolved}"
    )

    driver = GraphDatabase.driver(
        f"neo4j+s://{hostname}",
        auth=(neo4j_config["username"], neo4j_config["password"]),
        max_connection_lifetime=240,
        liveness_check_timeout=120,
        connection_acquisition_timeout=30,
        max_transaction_retry_time=30,
    )
    try:
        driver.verify_connectivity()
        records, summary, _ = driver.execute_query("RETURN 1 AS n")
        assert records[0]["n"] == 1
        print(f"\n  neo4j+s:// via PE ({resolved}) → App GW ({appgw_ip}) → Aura BC")
        print(f"  Server: {summary.server.address}")
        print("  ROUTING PROTOCOL WORKS through Application Gateway")
    finally:
        driver.close()


def test_neo4j_read_write_sessions(neo4j_config, appgw_ip):
    """Test neo4j+s:// with explicit read and write sessions.

    If routing works, the driver may direct reads and writes to different
    cluster members. Both should succeed through the gateway.
    """
    hostname = neo4j_config["hostname"]

    driver = GraphDatabase.driver(
        f"neo4j+s://{hostname}",
        auth=(neo4j_config["username"], neo4j_config["password"]),
        max_connection_lifetime=240,
        liveness_check_timeout=120,
        connection_acquisition_timeout=30,
        max_transaction_retry_time=30,
    )
    try:
        with driver.session(database="neo4j", default_access_mode="READ") as session:
            result = session.run("RETURN 1 AS test")
            assert result.single()["test"] == 1
            print(f"\n  neo4j+s:// READ session: PASS")

        with driver.session(database="neo4j", default_access_mode="WRITE") as session:
            result = session.run("RETURN 1 AS test")
            assert result.single()["test"] == 1
            print(f"  neo4j+s:// WRITE session: PASS")
    finally:
        driver.close()


# ---------------------------------------------------------------------------
# Direct baseline (bypasses PE, connects over public internet)
# ---------------------------------------------------------------------------

def test_bolt_direct_baseline(neo4j_config, allowlist_vm_ip):
    """Direct bolt+s:// to Aura BC (bypassing PE) to verify allowlisting works.

    Uses the real Aura IP (resolved externally) to bypass the /etc/hosts override.
    """
    hostname = neo4j_config["hostname"]

    import subprocess
    result = subprocess.run(
        ["dig", "+short", hostname, "@8.8.8.8"],
        capture_output=True, text=True, timeout=10,
    )
    real_ip = result.stdout.strip().splitlines()[-1] if result.returncode == 0 else None
    assert real_ip, f"Could not resolve real IP for {hostname} via external DNS"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    try:
        sock.connect((real_ip, 7687))
    finally:
        sock.close()

    driver = GraphDatabase.driver(
        f"bolt+s://{real_ip}",
        auth=(neo4j_config["username"], neo4j_config["password"]),
        trusted_certificates=False,
    )
    try:
        records, summary, _ = driver.execute_query("RETURN 1 AS n")
        assert records[0]["n"] == 1
        print(f"\n  Direct baseline: VM IP {allowlist_vm_ip} → Aura BC ({real_ip})")
        print(f"  Server: {summary.server.address}")
    finally:
        driver.close()
