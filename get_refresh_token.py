"""
Google Calendar のリフレッシュトークンを取得するスクリプト。
"""
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_CONFIG = {
    "web": {
        "client_id": "456075101393-d4vj3ajti961iattflc4688r36er4k70.apps.googleusercontent.com",
        "client_secret": "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",
        "redirect_uris": ["http://localhost:8080/"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

SCOPES = ["https://www.googleapis.com/auth/calendar"]

flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
creds = flow.run_local_server(port=8080)

print("\n=== 以下をVercelの環境変数に設定してください ===")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
print(f"GOOGLE_CLIENT_ID={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
