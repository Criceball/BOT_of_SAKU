"""Google ドライブのテキストファイル（読むだけ）から、質問に関係する行だけを取り出す。

環境変数 KNOWLEDGE_URL に、テキストファイルの共有リンク（リンクを知っている全員）を入れる。
ファイルは、一定時間メモリに残して、毎回ダウンロードしない。検索は文字の組み合わせで行うので、AI のトークンは使わない。
"""

import logging
import os
import re
import time

import aiohttp

log = logging.getLogger(__name__)

KNOWLEDGE_URL = os.getenv("KNOWLEDGE_URL", "")
CACHE_SECONDS = 600          # ファイルを読み直すまでの時間
MAX_LINES = 8                # AI に渡す行数の上限
MAX_CHARS = 900              # AI に渡す文字数の上限
# 日本語でよく出てくる、探す役に立たない2文字（ノイズになるので除く）
STOP_GRAMS = {"です", "ます", "ある", "ない", "する", "した", "して", "こと", "もの",
              "どう", "なに", "何か", "教え", "ください", "ですか", "ますか", "について"}

_cache: dict = {"lines": [], "at": 0.0}


def drive_download_url(link: str) -> str:
    m = re.search(r"/d/([\w-]+)", link) or re.search(r"[?&]id=([\w-]+)", link)
    if not m:
        return link
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


async def _download(url: str) -> str:
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    try:
        resp = await session.get(url)
        if "text/html" in resp.headers.get("Content-Type", ""):
            # 大きいファイルは、Google が確認の画面を出す。確認の印を付けて、もう一度取りに行く
            page = await resp.text()
            resp.release()
            m = re.search(r"confirm=([0-9A-Za-z_-]+)", page)
            resp = await session.get(f"{url}&confirm={m.group(1) if m else 't'}")
        if resp.status != 200:
            resp.release()
            raise RuntimeError(f"HTTP {resp.status}")
        raw = await resp.read()
        resp.release()
        return raw.decode("utf-8-sig", errors="replace")
    finally:
        await session.close()


async def load_lines() -> list[str]:
    if not KNOWLEDGE_URL:
        return []
    if time.monotonic() - _cache["at"] < CACHE_SECONDS and _cache["lines"]:
        return _cache["lines"]
    try:
        text = await _download(drive_download_url(KNOWLEDGE_URL))
        _cache["lines"] = [line.strip() for line in text.splitlines() if line.strip()]
        _cache["at"] = time.monotonic()
    except Exception as e:
        # 読めなかったときは、前に読んだ内容を使う（なければ空）
        log.warning("知識のファイルを読めませんでした: %s", e)
    return _cache["lines"]


def _grams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[i:i + 2] for i in range(len(compact) - 1)} - STOP_GRAMS


def search(query: str, lines: list[str]) -> list[str]:
    """質問の2文字の組み合わせが、多く含まれる行を選ぶ（上位だけ、順番どおりに返す）。"""
    wanted = _grams(query)
    if not wanted:
        return []
    need = 2 if len(wanted) >= 4 else 1
    scored = []
    for i, line in enumerate(lines):
        score = len(wanted & _grams(line))
        if score >= need:
            scored.append((-score, i, line))
    scored.sort()
    picked, total = [], 0
    for _, _, line in scored[:MAX_LINES]:
        if total + len(line) > MAX_CHARS:
            break
        picked.append(line)
        total += len(line)
    return picked


async def lookup(query: str) -> str | None:
    """質問に関係する行を、AI に渡す文字として返す。見つからなければ None。"""
    lines = await load_lines()
    if not lines:
        return None
    hits = search(query, lines)
    return "\n".join(hits) if hits else None
