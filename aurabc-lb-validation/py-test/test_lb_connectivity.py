"""
End-to-end tests for the Aura BC Load Balancer + Private Link setup.

Runs on the test VM deployed by deploy_test_vm.py. Traffic path:
  Test VM (eastus) → PE → PLS (westus3) → ILB → HAProxy → NAT GW → Aura BC

Usage (from the VM):
    uv run pytest -v -s
"""

import os
import socket

from neo4j import GraphDatabase


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


def test_bolt_through_private_endpoint(neo4j_config, nat_gw_ip):
    """Full bolt+s:// connection through PE → PLS → LB → HAProxy → NAT GW → Aura BC.

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
        print(f"\n  Connected via PE ({resolved}) → NAT GW ({nat_gw_ip}) → Aura BC")
        print(f"  Server: {summary.server.address}")
    finally:
        driver.close()


def test_bolt_direct_baseline(neo4j_config, allowlist_vm_ip):
    """Direct bolt+s:// to Aura BC (bypassing PE) to verify allowlisting works.

    Removes the /etc/hosts override and runs the driver in a separate subprocess
    so DNS resolves fresh (the parent process caches /etc/hosts entries in the
    C resolver for its lifetime). Uses the real Aura FQDN for valid TLS SNI.
    """
    import json
    import subprocess
    import textwrap

    hostname = neo4j_config["hostname"]
    pe_ip = os.environ.get("PE_IP", "")

    # Resolve real Aura IP via external DNS before touching /etc/hosts
    result = subprocess.run(
        ["dig", "+short", hostname, "@8.8.8.8"],
        capture_output=True, text=True, timeout=10,
    )
    real_ip = result.stdout.strip().splitlines()[-1] if result.returncode == 0 else None
    assert real_ip, f"Could not resolve real IP for {hostname} via external DNS"
    print(f"\n  Real Aura IP (via DNS): {real_ip}")

    # Remove /etc/hosts override
    subprocess.run(
        ["sudo", "sed", "-i", f"/{hostname}/d", "/etc/hosts"],
        check=True, timeout=10,
    )
    try:
        # Run the driver test in a subprocess (fresh process = no DNS cache)
        script = textwrap.dedent(f"""\
            import json, socket
            from neo4j import GraphDatabase

            hostname = "{hostname}"
            resolved = socket.gethostbyname(hostname)
            result = {{"resolved_ip": resolved}}

            if resolved.startswith("10."):
                result["error"] = f"{{hostname}} still resolves to PE IP {{resolved}} (DNS cache?)"
                print(json.dumps(result))
                exit(1)

            driver = GraphDatabase.driver(
                f"bolt+s://{{hostname}}",
                auth=("{neo4j_config['username']}", "{neo4j_config['password']}"),
            )
            try:
                records, summary, _ = driver.execute_query("RETURN 1 AS n")
                result["n"] = records[0]["n"]
                result["server"] = str(summary.server.address)
            except Exception as e:
                result["error"] = str(e)
            finally:
                driver.close()

            print(json.dumps(result))
        """)

        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=60,
        )

        # Parse result
        output = result.stdout.strip()
        assert output, f"Subprocess produced no output. stderr: {result.stderr.strip()}"
        data = json.loads(output)

        assert "error" not in data, f"Direct baseline failed: {data['error']}"
        assert data.get("n") == 1, f"Expected n=1, got {data}"

        print(f"  Resolved: {hostname} → {data['resolved_ip']}")
        print(f"  Direct baseline: VM IP {allowlist_vm_ip} → Aura BC ({data['resolved_ip']})")
        print(f"  Server: {data.get('server', 'unknown')}")
    finally:
        # Restore /etc/hosts entry
        if pe_ip:
            subprocess.run(
                f'echo "{pe_ip} {hostname}" | sudo tee -a /etc/hosts',
                shell=True, check=True, timeout=10,
            )
