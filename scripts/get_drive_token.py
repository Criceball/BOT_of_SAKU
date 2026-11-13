"""/deta の保存に使う、Google Drive の更新用トークン（refresh token）を一度だけ作る。

手元のパソコンで実行します（Render では実行しません）。
1. Google Cloud で「デスクトップアプリ」の OAuth クライアントを作り、client_secret.json として保存する
2. pip install google-auth-oauthlib
3. python scripts/get_drive_token.py client_secret.json
4. ブラウザが開くので、あなたのアカウントで許可する
5. 表示された3つの値を、Render の環境変数に入れる（チャットには貼らない）

このスクリプトが使うのは drive.file の権限だけです。ボットが作ったファイルしか触れません。
"""

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> None:
    if len(sys.argv) != 2:
        print("使い方: python scripts/get_drive_token.py client_secret.json")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    installed = json.load(open(sys.argv[1], encoding="utf-8")).get("installed", {})
    print("\n次の3つを、Render の環境変数に入れてください（秘密の値です。チャットには貼らないでください）:")
    print(f"GOOGLE_OAUTH_CLIENT_ID={installed.get('client_id', '')}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={installed.get('client_secret', '')}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
