"""
Validate bolt+s:// connectivity against Neo4j Aura Business Critical.

If bolt+s:// works, the Load Balancer + reverse proxy architecture is viable.
If it fails, we fall back to the stable NAT IP approach.
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


# Test 1: neo4j+s:// (control — should always work)
test_connection(
    "neo4j+s:// (control)",
    f"neo4j+s://{hostname}",
)

# Test 2: bolt+s:// (the scheme the LB reverse proxy would use)
test_connection(
    "bolt+s:// (LB scheme)",
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

# Summary
print("=" * 60)
print("SUMMARY")
print("=" * 60)
for label, passed in results.items():
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}")
print()

bolt_tests_passed = results.get("bolt+s:// (LB scheme)") and results.get("bolt+s:// + keepalive (PL timeouts)")
if bolt_tests_passed:
    print("bolt+s:// WORKS -- LB approach is viable")
else:
    print("bolt+s:// FAILED -- LB approach is NOT viable, recommend stable NAT IP approach")
