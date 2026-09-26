#!/usr/bin/env python3
"""Point native Cloud Run IAP (hr-agent) at a custom External OAuth client.

The Google-managed OAuth client only admits users of the project's own org, so
@google.com users are rejected. This script:
  1. stores IAP_OAUTH_CLIENT_SECRET in Secret Manager (hr-iap-oauth-client-secret),
  2. PATCHes hr-agent iapSettings.accessSettings.oauthSettings with the client id/secret.
The secret never touches Terraform state. Auth: Application Default Credentials.

Usage: uv run --env-file .env python deployment/set_iap_oauth.py
Needs IAP_OAUTH_CLIENT_ID and IAP_OAUTH_CLIENT_SECRET in the environment (.env, gitignored).
"""

import os
import sys

import google.auth
from google.api_core import exceptions
from google.auth.transport.requests import AuthorizedSession
from google.cloud import secretmanager

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "jm-01-project")
REGION = os.getenv("REGION", "asia-southeast1")
SERVICE = "hr-agent"
SECRET_ID = "hr-iap-oauth-client-secret"


def store_secret(value: str) -> None:
    sm = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{PROJECT}"
    name = f"{parent}/secrets/{SECRET_ID}"
    try:
        sm.create_secret(
            request={
                "parent": parent,
                "secret_id": SECRET_ID,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"created secret {SECRET_ID}")
    except exceptions.AlreadyExists:
        pass
    sm.add_secret_version(request={"parent": name, "payload": {"data": value.encode()}})
    print(f"added new version to {SECRET_ID}")


def patch_iap(client_id: str, client_secret: str) -> None:
    creds, _ = google.auth.default()
    s = AuthorizedSession(creds)
    res = f"projects/{PROJECT}/iap_web/cloud_run-{REGION}/services/{SERVICE}"
    # No updateMask: the API rejects field masks for oauthSettings on Cloud Run
    # resources (INVALID_ARGUMENT); a full update of the (otherwise empty) settings works.
    url = f"https://iap.googleapis.com/v1/{res}:iapSettings"
    body = {
        "accessSettings": {
            "oauthSettings": {"clientId": client_id, "clientSecret": client_secret}
        },
    }
    r = s.patch(url, json=body)
    if r.status_code != 200:
        sys.exit(f"iapSettings PATCH failed ({r.status_code}): {r.text}")
    got = r.json().get("accessSettings", {}).get("oauthSettings", {})
    print(f"IAP now uses OAuth client {got.get('clientId')}")


def main() -> None:
    cid = os.getenv("IAP_OAUTH_CLIENT_ID")
    secret = os.getenv("IAP_OAUTH_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set IAP_OAUTH_CLIENT_ID and IAP_OAUTH_CLIENT_SECRET in .env first.")
    store_secret(secret)
    patch_iap(cid, secret)


if __name__ == "__main__":
    main()
