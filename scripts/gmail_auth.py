#!/usr/bin/env python3
"""One-time Gmail OAuth2 setup script.

Run this locally once to generate a refresh token for job-watch.
It will open your browser, ask you to sign in with the Gmail account
that receives job alerts, and then print the three values you need
to store in a Kubernetes secret.

Prerequisites:
  1. Create a GCP project (or use an existing one).
  2. Enable the Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com
  3. Create an OAuth 2.0 client ID (type: Desktop app).
  4. Download the credentials JSON and pass it to this script with --credentials.

Usage:
  pip install google-auth-oauthlib
  python scripts/gmail_auth.py --credentials ~/Downloads/client_secret_....json

After successful auth, run the printed kubectl command to create the secret.
"""

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Gmail OAuth2 refresh token for job-watch.")
    parser.add_argument(
        "--credentials",
        required=True,
        metavar="PATH",
        help="Path to the OAuth2 client credentials JSON downloaded from GCP.",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib is not installed.")
        print("Run:  pip install google-auth-oauthlib")
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, scopes=scopes)
    creds = flow.run_local_server(port=0, open_browser=True)

    with open(args.credentials) as f:
        client_info = json.load(f)

    # Handle both "installed" and "web" credential types
    info = client_info.get("installed") or client_info.get("web") or {}
    client_id = info.get("client_id", creds.client_id)
    client_secret = info.get("client_secret", creds.client_secret)
    refresh_token = creds.refresh_token

    if not refresh_token:
        print("\nERROR: No refresh token returned.")
        print("In GCP, ensure 'Access type' is 'Offline' and revoke any existing consent first.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS — store these three values in a Kubernetes secret.")
    print("=" * 60)
    print(f"\n  Client ID:      {client_id}")
    print(f"  Client Secret:  {client_secret}")
    print(f"  Refresh Token:  {refresh_token}")
    print()
    print("Create the K8s secret (replace <namespace> with your namespace, e.g. job-watch):")
    print()
    print("  kubectl create secret generic job-watch-gmail-creds \\")
    print(f'    --from-literal=GMAIL_CLIENT_ID="{client_id}" \\')
    print(f'    --from-literal=GMAIL_CLIENT_SECRET="{client_secret}" \\')
    print(f'    --from-literal=GMAIL_REFRESH_TOKEN="{refresh_token}" \\')
    print("    -n <namespace>")
    print()
    print("Then reference it in your Helm values:")
    print()
    print("  envFrom:")
    print("    - secretRef:")
    print("        name: job-watch-gmail-creds")
    print()
    print("Finally, set gmail_discovery.enabled: true in companies.yaml.")
    print()


if __name__ == "__main__":
    main()
