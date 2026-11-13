"""/deta の取り出し結果を、Google ドライブの「/deta」フォルダに保存する。

- フォルダは、ボットが作ったものだけを使う（drive.file の権限では、ボットが作ったファイルしか見えません）。
- 同じ名前のファイルがあれば、上書きする。無ければ作る。
- フォルダは、あとからあなたのアカウントに閲覧権限で共有すると、中身を確認できます。
"""

import asyncio
import json
import logging

import aiohttp

from services import bot_drive
from services.bot_drive import BotDriveError

log = logging.getLogger(__name__)

FOLDER_NAME = "/deta"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILES_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
BOUNDARY = "deta_boundary_9f3c"

_folder_id: str | None = None


def _escape(value: str) -> str:
    """Drive の検索式の中に入れるため、\\ と ' をエスケープする。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _list(session: aiohttp.ClientSession, headers: dict, query: str) -> list[dict]:
    params = {"q": query, "fields": "files(id,name)", "pageSize": "10", "spaces": "drive"}
    async with session.get(FILES_API, params=params, headers=headers) as resp:
        if resp.status >= 400:
            raise BotDriveError(f"Drive の検索に失敗しました（{resp.status}）。")
        data = await resp.json()
    return data.get("files", [])


async def ensure_folder() -> str:
    """「/deta」フォルダの ID を返す。無ければ作る。"""
    global _folder_id
    if _folder_id:
        return _folder_id
    token = await asyncio.to_thread(bot_drive.access_token)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        query = f"name = '{_escape(FOLDER_NAME)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        found = await _list(session, headers, query)
        if found:
            _folder_id = found[0]["id"]
            return _folder_id
        body = {"name": FOLDER_NAME, "mimeType": FOLDER_MIME}
        async with session.post(FILES_API, params={"fields": "id"}, json=body, headers=headers) as resp:
            if resp.status >= 400:
                raise BotDriveError(f"/deta フォルダを作れませんでした（{resp.status}）。")
            _folder_id = (await resp.json())["id"]
    log.info("[Drive] /deta フォルダを作りました")
    return _folder_id


async def save_text(name: str, data: bytes) -> str:
    """フォルダに name のテキストを保存する（同じ名前があれば上書き）。ファイルの ID を返す。"""
    folder_id = await ensure_folder()
    token = await asyncio.to_thread(bot_drive.access_token)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        query = f"name = '{_escape(name)}' and '{folder_id}' in parents and trashed = false"
        found = await _list(session, headers, query)
        if found:
            file_id = found[0]["id"]
            put_headers = {**headers, "Content-Type": "text/plain; charset=utf-8"}
            async with session.patch(
                f"{UPLOAD_API}/{file_id}", params={"uploadType": "media"}, data=data, headers=put_headers
            ) as resp:
                if resp.status >= 400:
                    raise BotDriveError(f"{name} を上書きできませんでした（{resp.status}）。")
            return file_id

        metadata = json.dumps({"name": name, "parents": [folder_id], "mimeType": "text/plain"})
        body = (
            f"--{BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
            f"--{BOUNDARY}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
        post_headers = {**headers, "Content-Type": f"multipart/related; boundary={BOUNDARY}"}
        async with session.post(
            UPLOAD_API, params={"uploadType": "multipart", "fields": "id"}, data=body, headers=post_headers
        ) as resp:
            if resp.status >= 400:
                raise BotDriveError(f"{name} を保存できませんでした（{resp.status}）。")
            return (await resp.json())["id"]


async def find_folder_id() -> str | None:
    """「/deta」フォルダの ID を返す。無ければ None（作らない。検索のときに使う）。"""
    if _folder_id:
        return _folder_id
    token = await asyncio.to_thread(bot_drive.access_token)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        query = f"name = '{_escape(FOLDER_NAME)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        found = await _list(session, headers, query)
    return found[0]["id"] if found else None


async def read_history_files() -> dict[str, bytes]:
    """「/deta」フォルダの「-history.txt」を全部読む。フォルダが無ければ空の辞書。"""
    folder_id = await find_folder_id()
    if not folder_id:
        return {}
    token = await asyncio.to_thread(bot_drive.access_token)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=300)
    result: dict[str, bytes] = {}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        query = f"'{folder_id}' in parents and trashed = false and name contains '-history.txt'"
        params = {"q": query, "fields": "files(id,name)", "pageSize": "200", "spaces": "drive"}
        async with session.get(FILES_API, params=params, headers=headers) as resp:
            if resp.status >= 400:
                raise BotDriveError(f"/deta フォルダの一覧を読めませんでした（{resp.status}）。")
            files = (await resp.json()).get("files", [])
        for item in files:
            async with session.get(f"{FILES_API}/{item['id']}", params={"alt": "media"}, headers=headers) as resp:
                if resp.status >= 400:
                    log.warning("[Drive] %s を読めませんでした: %s", item["name"], resp.status)
                    continue
                result[item["name"]] = await resp.read()
    return result
