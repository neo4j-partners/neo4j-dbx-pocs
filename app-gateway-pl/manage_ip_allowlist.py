"""
Manage IP allowlist on a Neo4j Aura BC instance via the Aura Admin API (v2beta1).

POC tool for adding/removing Application Gateway IPs during validation testing.

Usage:
    uv run python manage_ip_allowlist.py list
    uv run python manage_ip_allowlist.py add --ip 20.42.0.10 --description "App Gateway IP"
    uv run python manage_ip_allowlist.py remove --filter-id <filter-id>
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.neo4j.io/v2beta1"
TOKEN_URL = "https://api.neo4j.io/oauth/token"

CLIENT_ID = os.environ.get("AURA_API_CLIENT_ID")
CLIENT_SECRET = os.environ.get("AURA_API_CLIENT_SECRET")
ORG_ID = os.environ.get("AURA_ORG_ID")
INSTANCE_ID = os.environ.get("AURA_INSTANCE_ID")


def get_token():
    """Get an OAuth2 access token using client credentials."""
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def auth_headers():
    token = get_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def list_ip_filters():
    """List all IP filters for the organization."""
    url = f"{API_BASE}/organizations/{ORG_ID}/ip-filters"
    resp = requests.get(url, headers=auth_headers(), timeout=45)
    resp.raise_for_status()
    body = resp.json()
    filters = body if isinstance(body, list) else body.get("data", [])

    if not filters:
        print("No IP filters found.")
        return

    for f in filters:
        print(f"\n--- Filter: {f['name']} (ID: {f['id']}) ---")
        if f.get("description"):
            print(f"  Description: {f['description']}")

        entities = f.get("filtered_entities", {})
        if entities.get("instances"):
            print(f"  Instances: {', '.join(entities['instances'])}")
        if entities.get("projects"):
            print(f"  Projects: {', '.join(entities['projects'])}")
        if entities.get("organizations"):
            print(f"  Organizations: {', '.join(entities['organizations'])}")

        allow_list = f.get("allow_list", [])
        if allow_list:
            print(f"  Allow list ({len(allow_list)} entries):")
            for entry in allow_list:
                desc = entry.get("description", "")
                desc_str = f" -- {desc}" if desc else ""
                ip_range = entry.get("ip_range") or f"{entry.get('address', '?')}/{entry.get('prefix_len', '?')}"
                print(f"    {ip_range}{desc_str}")
        else:
            print("  Allow list: (empty)")


def add_ip_to_allowlist(ip_address, description):
    """Create an IP filter that allows a single IP (/32) on the configured instance."""
    url = f"{API_BASE}/organizations/{ORG_ID}/ip-filters"
    cidr = ip_address if "/" in ip_address else f"{ip_address}/32"
    name = f"AppGW-POC-{ip_address}"[:30]
    payload = {
        "name": name,
        "organization_id": ORG_ID,
        "allow_list": [
            {
                "ip_range": cidr,
                "description": description,
            }
        ],
        "filtered_entities": {
            "instances": [INSTANCE_ID],
            "projects": [],
            "organizations": [],
        },
    }

    resp = requests.post(url, headers=auth_headers(), json=payload, timeout=45)
    if not resp.ok:
        print(f"  HTTP {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    result = resp.json()

    filter_data = result.get("data", result) if isinstance(result, dict) else result
    filter_id = filter_data.get("id", "unknown") if isinstance(filter_data, dict) else "unknown"
    print(f"Created IP filter for {ip_address}/32 on instance {INSTANCE_ID}")
    print(f"Filter ID: {filter_id}")
    print(f"Description: {description}")


def remove_ip_filter(filter_id):
    """Delete an IP filter by ID."""
    url = f"{API_BASE}/organizations/{ORG_ID}/ip-filters/{filter_id}"
    resp = requests.delete(url, headers=auth_headers(), timeout=45)
    resp.raise_for_status()
    print(f"Deleted IP filter: {filter_id}")


def check_env():
    """Verify required environment variables are set."""
    missing = []
    for var in ["AURA_API_CLIENT_ID", "AURA_API_CLIENT_SECRET", "AURA_ORG_ID", "AURA_INSTANCE_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"Error: Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy .env.sample to .env and fill in the values.", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Manage Neo4j Aura BC IP allowlist")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List all IP filters for the organization")

    add_parser = subparsers.add_parser("add", help="Add an IP to the instance allowlist")
    add_parser.add_argument("--ip", required=True, help="IP address to allow (added as /32)")
    add_parser.add_argument("--description", required=True, help="Description for the IP entry")

    remove_parser = subparsers.add_parser("remove", help="Remove an IP filter by ID")
    remove_parser.add_argument("--filter-id", required=True, help="ID of the IP filter to delete")

    args = parser.parse_args()
    check_env()

    if args.command == "list":
        list_ip_filters()
    elif args.command == "add":
        add_ip_to_allowlist(args.ip, args.description)
    elif args.command == "remove":
        remove_ip_filter(args.filter_id)


if __name__ == "__main__":
    main()
