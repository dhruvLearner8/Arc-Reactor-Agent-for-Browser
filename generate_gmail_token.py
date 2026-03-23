"""
One-time setup: generate token.json from credentials.json for Gmail API.
Run: uv run python generate_gmail_token.py
Place credentials.json (from Google Cloud Console, Gmail API enabled) in project root first.
"""
from lib.gmail_api import SCOPES, _CREDENTIALS_PATH, _TOKEN_PATH
from google_auth_oauthlib.flow import InstalledAppFlow

if __name__ == "__main__":
    if not _CREDENTIALS_PATH.exists():
        print(f"Put credentials.json in {_CREDENTIALS_PATH.parent}")
        exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    with open(_TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"Saved token to {_TOKEN_PATH}")
