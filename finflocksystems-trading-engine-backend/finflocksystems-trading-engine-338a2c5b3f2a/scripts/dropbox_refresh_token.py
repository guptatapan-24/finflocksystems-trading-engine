#!/usr/bin/env python3
"""One-time helper: turn your Dropbox app key + secret into a permanent REFRESH TOKEN.

Run it yourself (it asks you to log in to Dropbox in a browser and paste a code):
    python dropbox_refresh_token.py
The Dropbox app needs the scopes files.metadata.read and sharing.read (App console > Permissions,
set BEFORE running this). Put the printed token in secrets.plain.env as DROPBOX_REFRESH_TOKEN.
Nothing is stored by this script.
"""
import getpass
import urllib.parse

import requests

key = input("Dropbox app key: ").strip()
secret = getpass.getpass("Dropbox app secret (hidden): ").strip()
url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
    {"client_id": key, "response_type": "code", "token_access_type": "offline"})
print("\n1) Open this link, click Allow, and copy the code Dropbox shows:\n\n   " + url + "\n")
code = input("2) Paste the code here: ").strip()
reply = requests.post("https://api.dropboxapi.com/oauth2/token", data={"code": code, "grant_type": "authorization_code"},
                      auth=(key, secret), timeout=30)
if reply.status_code != 200:
    raise SystemExit(f"Dropbox refused the code (HTTP {reply.status_code}). A code works once; start again.")
body = reply.json()
print("\nGranted scopes:", body.get("scope"))
print("DROPBOX_REFRESH_TOKEN=" + body["refresh_token"])
