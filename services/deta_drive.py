"""Google ドライブの「/deta」フォルダから、会話の記録（テキストファイル）を読む。読むだけで、書き込みはしない。

- フォルダは、あなたが作って、サービスアカウントに閲覧者として共有してください。
- 環境変数 DETA_FOLDER_ID に、そのフォルダの ID を入れます（URL の folders/ の後ろの部分）。
- 中には、.txt のテキストファイルを入れてください（Google ドキュメントではなく、テキストファイルとして）。
"""

import asyncio
import logging
import os

import aiohttp

from services import bot_drive
from services.bot_drive import BotDriveError

log = logging.getLogger(__name__)

FILES_API = "https://www.googleapis.com/drive/v3/files"


def configured() -> bool:
    return bool(os.getenv("DETA_FOLDER_ID", "").strip() and os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())


async def read_history_files() -> dict[str, bytes]:
    """/deta フォルダの .txt を全部読む。ファイル名 → 中身 の辞書で返す。"""
    if not configured():
        return {}
    folder_id = os.environ["DETA_FOLDER_ID"].strip()
    token = await asyncio.to_thread(bot_drive.access_token)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=300)
    result: dict[str, bytes] = {}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        listing: list[dict] = []
        page_token = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false and name contains '.txt'",
                "fields": "nextPageToken,files(id,name)",
                "pageSize": "200",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            async with session.get(FILES_API, params=params, headers=headers) as resp:
                if resp.status >= 400:
                    raise BotDriveError(f"/deta フォルダを読めませんでした（{resp.status}）。サービスアカウントに共有されているか確かめてください。")
                data = await resp.json()
            listing.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        for item in listing:
            async with session.get(f"{FILES_API}/{item['id']}", params={"alt": "media"}, headers=headers) as resp:
                if resp.status >= 400:
                    log.warning("[/deta] %s を読めませんでした: %s", item["name"], resp.status)
                    continue
                result[item["name"]] = await resp.read()
    return result
