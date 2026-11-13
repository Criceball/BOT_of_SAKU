"""Google ドライブの bot 専用フォルダから、設定と資料を読む（サービスアカウントで、読むだけ）。

環境変数:
- GOOGLE_SERVICE_ACCOUNT_JSON：サービスアカウントの鍵（JSON の中身をそのまま入れる）
- BOT_DRIVE_FOLDER_ID：フォルダの ID（フォルダの URL の最後の部分）

フォルダには、次の名前のファイルを置く（ほかの名前は読まない）。
- persona.txt（性格）、web_persona.txt（Web画面の性格）、club.txt（部活の情報）
- knowledge.txt（参考資料）、web_package.zip（Web画面の言葉・動画のパッケージ）

テキストは、Google ドキュメントではなく、テキストファイル（.txt）としてアップロードしてください。
読み直しは /load コマンド、または起動のときに行います。
"""

import asyncio
import json
import logging
import os

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2 import service_account

log = logging.getLogger(__name__)

# drive.readonly: 共有されたフォルダの資料を読む。drive.file: ボットが作ったファイル（/deta）だけを読み書きする
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
SUPPORTED_NAMES = {"persona.txt", "web_persona.txt", "club.txt", "knowledge.txt", "web_package.zip"}
DRIVE_API = "https://www.googleapis.com/drive/v3/files"

_files: dict[str, bytes] = {}


class BotDriveError(Exception):
    """ユーザーに説明できる失敗。"""


def configured() -> bool:
    return bool(os.getenv("BOT_DRIVE_FOLDER_ID", "").strip() and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())


def get_text(name: str) -> str | None:
    """読み込んだテキストを返す（無いとき、空のとき None）。"""
    data = _files.get(name)
    if data is None:
        return None
    text = data.decode("utf-8-sig").strip()
    return text or None


def get_bytes(name: str) -> bytes | None:
    return _files.get(name)


def access_token() -> str:
    """Google API を呼ぶためのアクセストークン（同期の処理。to_thread で呼ぶ）。"""
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


async def reload() -> dict[str, int]:
    """フォルダを読み直す。読めたファイル名とバイト数を返す。失敗したときは、前の内容を残す。"""
    if not configured():
        raise BotDriveError("Google ドライブの設定（BOT_DRIVE_FOLDER_ID と GOOGLE_SERVICE_ACCOUNT_JSON）がありません。")
    folder_id = os.environ["BOT_DRIVE_FOLDER_ID"].strip()
    token = await asyncio.to_thread(access_token)
    headers = {"Authorization": f"Bearer {token}"}

    timeout = aiohttp.ClientTimeout(total=300)
    new_files: dict[str, bytes] = {}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        listing: list[dict] = []
        page_token = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name)",
                "pageSize": "200",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            async with session.get(DRIVE_API, params=params, headers=headers) as resp:
                if resp.status >= 400:
                    log.warning("フォルダを読めませんでした: %s", resp.status)
                    raise BotDriveError(f"フォルダを読めませんでした（{resp.status}）。フォルダをサービスアカウントに共有しているか確かめてください。")
                data = await resp.json()
            listing.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        for item in listing:
            if item.get("name") not in SUPPORTED_NAMES:
                continue
            url = f"{DRIVE_API}/{item['id']}"
            async with session.get(url, params={"alt": "media", "supportsAllDrives": "true"}, headers=headers) as resp:
                if resp.status >= 400:
                    raise BotDriveError(f"{item['name']} を読めませんでした（{resp.status}）。")
                new_files[item["name"]] = await resp.read()

    _files.clear()
    _files.update(new_files)
    return {name: len(data) for name, data in sorted(new_files.items())}
