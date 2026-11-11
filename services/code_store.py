"""/code で作ったページの保存、利用回数の上限、表示ページ（/view/<id>、/raw/<id>）。

- 保存：Upstash Redis に7日間置く（設定が無いときはメモリだけに残す。再起動で消える）。
- 上限：1人1日 PER_USER_DAILY 回、全体で1日 GLOBAL_DAILY 回まで。
- 表示：/raw/<id> は、CSP の sandbox を付けて返す（ページの中のコードが、このサイトの情報に触れないように）。
"""

import html as html_lib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web

from services import usage_store

log = logging.getLogger(__name__)

PER_USER_DAILY = int(os.getenv("CODE_PER_USER_DAILY", "20"))
GLOBAL_DAILY = int(os.getenv("CODE_GLOBAL_DAILY", "200"))
PAGE_TTL = 7 * 86400
COUNT_TTL = 40 * 86400
MEMORY_LIMIT = 200

JST = timezone(timedelta(hours=9))
_pages: dict[str, str] = {}
_counts: dict[str, int] = {}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,30}$")


class QuotaError(Exception):
    """上限に達したときの説明。"""


def _today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


async def _incr(key: str, amount: int = 1) -> int:
    if usage_store.enabled():
        try:
            res = await usage_store._run([["INCRBY", key, amount], ["EXPIRE", key, COUNT_TTL]])
            return int(res[0])
        except Exception as e:
            log.warning("回数を保存できませんでした（メモリに残します）: %s", e)
    _counts[key] = _counts.get(key, 0) + amount
    return _counts[key]


async def take_quota(user_id: int) -> list[str]:
    """1回分の枠を取る。取れたら、戻すときに使うキーを返す。上限なら QuotaError。"""
    day = _today()
    user_key = f"cq:{day}:{user_id}"
    all_key = f"cq:{day}:all"

    user_n = await _incr(user_key)
    if user_n > PER_USER_DAILY:
        await _incr(user_key, -1)
        raise QuotaError(f"今日の作成回数（1人{PER_USER_DAILY}回）を使い切りました。明日また試してください。")

    all_n = await _incr(all_key)
    if all_n > GLOBAL_DAILY:
        await _incr(all_key, -1)
        await _incr(user_key, -1)
        raise QuotaError("今日はみんなの作成回数が上限に達しました。明日また試してください。")

    return [user_key, all_key]


async def refund_quota(keys: list[str]) -> None:
    """失敗したときに、使った枠を戻す。"""
    for key in keys:
        await _incr(key, -1)


def _page_key(page_id: str) -> str:
    return f"code:{page_id}"


async def save_page(html_text: str) -> str:
    page_id = secrets.token_urlsafe(9)
    if usage_store.enabled():
        try:
            await usage_store._run([["SET", _page_key(page_id), html_text, "EX", PAGE_TTL]])
            return page_id
        except Exception as e:
            log.warning("ページを保存できませんでした（メモリに残します）: %s", e)
    if len(_pages) >= MEMORY_LIMIT:
        _pages.pop(next(iter(_pages)))
    _pages[page_id] = html_text
    return page_id


async def load_page(page_id: str) -> str | None:
    if not _ID_RE.match(page_id):
        return None
    if usage_store.enabled():
        try:
            res = await usage_store._run([["GET", _page_key(page_id)]])
            if res and res[0]:
                return res[0]
        except Exception as e:
            log.warning("ページを読めませんでした: %s", e)
    return _pages.get(page_id)


def page_title(html_text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    return match.group(1).strip() if match else "作ったページ"


async def view_page(request: web.Request) -> web.StreamResponse:
    page_id = request.match_info["id"]
    page = await load_page(page_id)
    if page is None:
        raise web.HTTPNotFound(text="ページが見つかりません（期限が切れたかもしれません）")
    title = html_lib.escape(page_title(page))
    safe_id = html_lib.escape(page_id)
    body = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
html,body{{margin:0;height:100%;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;color:#1d1d1f}}
header{{display:flex;align-items:center;justify-content:space-between;height:48px;padding:0 16px;box-sizing:border-box;background:#fff;border-bottom:1px solid #e5e5ea}}
header a{{color:#0071e3;text-decoration:none;font-size:14px}}
iframe{{display:block;border:0;width:100%;height:calc(100% - 48px);background:#fff}}
</style></head>
<body>
<header><span>{title}</span><a href="/raw/{safe_id}" target="_blank" rel="noopener">別のタブで開く</a></header>
<iframe src="/raw/{safe_id}" sandbox="allow-scripts allow-forms" title="{title}"></iframe>
</body></html>"""
    return web.Response(text=body, content_type="text/html", charset="utf-8")


async def raw_page(request: web.Request) -> web.StreamResponse:
    page = await load_page(request.match_info["id"])
    if page is None:
        raise web.HTTPNotFound(text="ページが見つかりません（期限が切れたかもしれません）")
    headers = {
        # sandbox：このページの中のコードは、サイトの情報（保存領域・アクセスキーなど）に触れない
        "Content-Security-Policy": "sandbox allow-scripts allow-forms",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return web.Response(text=page, content_type="text/html", charset="utf-8", headers=headers)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/view/{id}", view_page)
    app.router.add_get("/raw/{id}", raw_page)
