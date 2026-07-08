#!/usr/bin/env python3
"""
deputy_oauth_setup.py — RUN ONCE, manually, to bootstrap Deputy OAuth.

Before running this:
  1. Register an OAuth client at:
       https://{install}.{geo}.deputy.com/exec/devapp/oauth_clients
     Note the Client ID and Client Secret.

  2. In a browser, logged in as the Deputy account that should own this
     integration, visit:
       https://once.deputy.com/my/oauth/login?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost&response_type=code&scope=longlife_refresh_token
     Approve access. You'll be redirected to a "this site can't be reached"
     page at http://localhost?code=XXXX — that's expected, nothing needs to
     be listening there. Copy the "code" value out of the address bar.

  3. You have 10 minutes to use that code before it expires — run this
     script promptly.

Usage:
    python3 deputy_oauth_setup.py <client_id> <client_secret> <code>

This writes deputy_token_store.json with the access_token, refresh_token,
and endpoint (your specific Deputy install URL). The main sync script reads
and rewrites this file automatically from then on — you should not need to
run this setup script again unless the refresh token itself gets revoked.
"""

import sys
import json
import requests

TOKEN_STORE_PATH = "deputy_token_store.json"

def main():
    if len(sys.argv) != 4:
        sys.exit("Usage: python3 deputy_oauth_setup.py <client_id> <client_secret> <code>")

    client_id, client_secret, code = sys.argv[1], sys.argv[2], sys.argv[3]

    resp = requests.post("https://once.deputy.com/my/oauth/access_token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": "http://localhost",
        "scope": "longlife_refresh_token",
    })

    if not resp.ok:
        print("Exchange failed:", resp.status_code, resp.text)
        print("\nCommon causes: the code expired (10 minute window), the code was")
        print("already used once, or client_id/client_secret don't match what you")
        print("registered at .../exec/devapp/oauth_clients.")
        sys.exit(1)

    data = resp.json()
    required = ["access_token", "refresh_token", "endpoint"]
    missing = [k for k in required if k not in data]
    if missing:
        print("Unexpected response shape — missing:", missing)
        print("Full response:", json.dumps(data, indent=2))
        print("\nCheck this against the current Deputy OAuth docs — the field")
        print("names may differ from what this script assumes.")
        sys.exit(1)

    store = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "endpoint": data["endpoint"],  # e.g. https://carriageworks.au.deputy.com
    }
    with open(TOKEN_STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)

    print(f"Success — wrote {TOKEN_STORE_PATH}")
    print(f"Deputy install endpoint: {data['endpoint']}")
    print("\nThe main sync script will read and automatically refresh this")
    print("file from now on. Keep it as secure as any other credential —")
    print("restrict file permissions and never commit it to version control.")


if __name__ == "__main__":
    main()
