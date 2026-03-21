"""
Inspect the Neo4j routing table returned by Aura Business Critical.

The critical question: when the neo4j+s:// driver fetches its routing table,
what hostnames and ports come back? If they differ from the connection FQDN,
they need to resolve through the private link chain for client-side routing
to work through the tunnel.

Usage:
    uv run python routing_poc/inspect_routing_table.py
"""

import os
import socket
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

URI = os.getenv("NEO4J_URI")
USERNAME = os.getenv("NEO4J_USERNAME")
PASSWORD = os.getenv("NEO4J_PASSWORD")

if not all([URI, USERNAME, PASSWORD]):
    print("ERROR: Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD in .env")
    sys.exit(1)

hostname = urlparse(URI).hostname
if not hostname:
    print(f"ERROR: Could not extract hostname from URI: {URI}")
    sys.exit(1)

print(f"Connection FQDN: {hostname}")
print(f"Connection URI:  {URI}")
print()


# -- Step 1: Resolve the connection FQDN ---------------------------------

print("=" * 60)
print("STEP 1: DNS RESOLUTION OF CONNECTION FQDN")
print("=" * 60)
connection_ips = set()
try:
    results = socket.getaddrinfo(hostname, 7687, socket.AF_INET)
    connection_ips = sorted(set(addr[4][0] for addr in results))
    for ip in connection_ips:
        print(f"  {hostname} -> {ip}")
except Exception as e:
    print(f"  DNS lookup failed: {e}")
print()


# -- Step 2: Connect with neo4j+s:// and read routing table --------------

print("=" * 60)
print("STEP 2: ROUTING TABLE (via driver internals)")
print("=" * 60)

all_hostnames = set()
routing_entries = {"routers": [], "readers": [], "writers": []}

try:
    driver = GraphDatabase.driver(
        f"neo4j+s://{hostname}",
        auth=(USERNAME, PASSWORD),
    )
    # Force routing table population
    driver.execute_query("RETURN 1 AS n")

    pool = driver._pool
    if hasattr(pool, "routing_tables"):
        for db, table in pool.routing_tables.items():
            print(f"  Database: {db}")
            print(f"  TTL:      {table.ttl}s")
            print()

            for role in ("routers", "readers", "writers"):
                addrs = getattr(table, role, [])
                print(f"  {role.capitalize()}:")
                if not addrs:
                    print("    (none)")
                for addr in addrs:
                    host, port = addr[0], addr[1]
                    all_hostnames.add(host)
                    routing_entries[role].append((host, port))
                    print(f"    {host}:{port}")
                print()
    else:
        print("  Could not access routing_tables from pool.")
        print(f"  Pool type: {type(pool).__name__}")

    driver.close()
except Exception as e:
    print(f"  Failed: {e}")
print()


# -- Step 3: SHOW SERVERS for cluster topology ----------------------------

print("=" * 60)
print("STEP 3: CLUSTER TOPOLOGY (via SHOW SERVERS)")
print("=" * 60)

try:
    driver = GraphDatabase.driver(
        f"neo4j+s://{hostname}",
        auth=(USERNAME, PASSWORD),
    )
    records, summary, keys = driver.execute_query("SHOW SERVERS")
    if not records:
        print("  (no results)")
    for record in records:
        data = dict(record)
        print(f"  Name:    {data.get('name', 'N/A')}")
        print(f"  Address: {data.get('address', 'N/A')}")
        print(f"  State:   {data.get('state', 'N/A')}")
        print(f"  Health:  {data.get('health', 'N/A')}")
        address = data.get("address")
        if address and ":" in address:
            all_hostnames.add(address.split(":")[0])
        elif address:
            all_hostnames.add(address)
        print()
    driver.close()
except Exception as e:
    print(f"  Failed (may not be available on Aura): {e}")
print()


# -- Step 4: DNS resolve every hostname from the routing table ------------

print("=" * 60)
print("STEP 4: DNS RESOLUTION OF ALL ROUTING TABLE HOSTNAMES")
print("=" * 60)

resolved = {}
if all_hostnames:
    for h in sorted(all_hostnames):
        try:
            results = socket.getaddrinfo(h, 7687, socket.AF_INET)
            ips = sorted(set(addr[4][0] for addr in results))
            resolved[h] = ips
            same = h == hostname
            for ip in ips:
                label = "connection FQDN" if same else "DIFFERENT HOST"
                print(f"  {h} -> {ip}  ({label})")
        except socket.gaierror as e:
            resolved[h] = None
            print(f"  {h} -> DNS FAILED: {e}")
else:
    print("  No hostnames collected from routing table.")
print()


# -- Step 5: Analysis ----------------------------------------------------

print("=" * 60)
print("ANALYSIS")
print("=" * 60)

if not all_hostnames:
    print("  Could not retrieve routing table entries for analysis.")
    sys.exit(1)

print(f"  Connection FQDN:         {hostname}")
print(f"  Unique routing hosts:    {len(all_hostnames)}")
print(f"  Routing table hostnames: {sorted(all_hostnames)}")
print()

if all_hostnames == {hostname}:
    print("  RESULT: All routing table entries use the connection FQDN.")
    print()
    print("  The neo4j+s:// driver will only attempt connections back to")
    print(f"  {hostname}, which is the same address used for the initial")
    print("  connection. A single tunnel endpoint can carry all traffic.")
    print()
    print("  Implications:")
    print("    - SNI routing: one hostname means SNI inspection is not needed")
    print("      for routing table entries (still needed for bolt vs HTTP)")
    print("    - Dual-LB: both LBs can target the same FQDN on different ports")
    print("    - Client-side routing through the tunnel is LIKELY FEASIBLE")
else:
    extra = all_hostnames - {hostname}
    print(f"  RESULT: Routing table contains hostnames beyond the connection FQDN.")
    print()
    print(f"  Additional hostnames: {sorted(extra)}")
    print()
    print("  The neo4j+s:// driver will attempt connections to these hosts")
    print("  after fetching the routing table. If these hosts resolve to IPs")
    print("  outside the private link chain, client-side routing will fail.")
    print()
    print("  Implications:")
    print("    - Each hostname needs DNS resolution through the tunnel")
    print("    - SNI routing: HAProxy must handle SNI for each hostname")
    print("    - Dual-LB: may need additional LBs or DNS overrides per host")
    print()

    # Check if all routing hostnames resolve to the same IPs
    all_ips = set()
    for h in all_hostnames:
        if resolved.get(h):
            all_ips.update(resolved[h])

    if len(all_ips) == 1:
        print("  NOTE: All hostnames resolve to the same IP. This may mean Aura")
        print("  uses DNS aliases for cluster members behind a single endpoint.")
        print("  Tunneling may still work if DNS is overridden locally.")
    elif all_ips:
        print(f"  NOTE: Hostnames resolve to {len(all_ips)} distinct IPs: {sorted(all_ips)}")
        print("  Each IP represents a separate destination that must be reachable")
        print("  through the private link chain.")
