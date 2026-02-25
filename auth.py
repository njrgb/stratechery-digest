"""
Run this once locally to authorize Gmail send and read access.
It will open a browser, ask you to log in and approve, then save token.json.
token.json contains your refresh token — keep it secret.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

with open('token.json', 'w') as f:
    f.write(creds.to_json())

print("Done! token.json saved.")
print("Add its contents as the GMAIL_TOKEN_JSON secret in GitHub Actions.")
