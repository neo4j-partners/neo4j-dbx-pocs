"""
Validate bolt+s:// and neo4j+s:// connectivity against Neo4j Aura Business Critical.

Tests both connection schemes to determine which works through the
Application Gateway TCP proxy. bolt+s:// is the known-working scheme;
neo4j+s:// triggers routing table discovery and may bypass the gateway.
"""

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

URI = os.getenv("NEO4J_URI")
USERNAME = os.getenv("NEO4J_USERNAME")
PASSWORD = os.getenv("NEO4J_PASSWORD")

if not all([URI, USERNAME, PASSWORD]):
    print("ERROR: Missing environment variables. Ensure .env contains NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD.")
    sys.exit(1)

hostname = urlparse(URI).hostname
if not hostname:
    print(f"ERROR: Could not extract hostname from URI: {URI}")
    sys.exit(1)

print(f"Target hostname: {hostname}")
print(f"Original URI: {URI}")
print()

results = {}


def test_connection(label, uri, **driver_kwargs):
    """Connect, run a trivial query, report success or failure."""
    print(f"--- Test: {label} ---")
    print(f"  URI: {uri}")
    if driver_kwargs:
        print(f"  Driver kwargs: {driver_kwargs}")
    try:
        driver = GraphDatabase.driver(uri, auth=(USERNAME, PASSWORD), **driver_kwargs)
        records, summary, keys = driver.execute_query("RETURN 1 AS n")
        value = records[0]["n"]
        driver.close()
        print(f"  Result: SUCCESS (returned n={value})")
        print(f"  Server: {summary.server.address}")
        results[label] = True
    except Exception as e:
        print(f"  Result: FAILED")
        print(f"  Error: {e}")
        results[label] = False
    print()


# Test 1: neo4j+s:// (routing protocol — may bypass gateway)
test_connection(
    "neo4j+s:// (routing protocol)",
    f"neo4j+s://{hostname}",
)

# Test 2: bolt+s:// (direct protocol — known working through intermediaries)
test_connection(
    "bolt+s:// (direct protocol)",
    f"bolt+s://{hostname}",
)

# Test 3: bolt+s:// with keepalive settings for Private Link timeout constraints
test_connection(
    "bolt+s:// + keepalive (PL timeouts)",
    f"bolt+s://{hostname}",
    max_connection_lifetime=240,
    liveness_check_timeout=120,
    connection_acquisition_timeout=30,
)

# Test 4: neo4j+s:// with keepalive settings
test_connection(
    "neo4j+s:// + keepalive (PL timeouts)",
    f"neo4j+s://{hostname}",
    max_connection_lifetime=240,
    liveness_check_timeout=120,
    connection_acquisition_timeout=30,
    max_transaction_retry_time=30,
)

# Summary
print("=" * 60)
print("SUMMARY")
print("=" * 60)
for label, passed in results.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}")
print()

bolt_passed = results.get("bolt+s:// (direct protocol)") and results.get("bolt+s:// + keepalive (PL timeouts)")
neo4j_passed = results.get("neo4j+s:// (routing protocol)") and results.get("neo4j+s:// + keepalive (PL timeouts)")

if neo4j_passed:
    print("neo4j+s:// WORKS -- full routing protocol available through Application Gateway")
elif bolt_passed:
    print("bolt+s:// WORKS -- Application Gateway approach is viable (direct protocol only)")
else:
    print("BOTH SCHEMES FAILED -- check Application Gateway configuration and Aura BC allowlist")
