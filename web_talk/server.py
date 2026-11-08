"""ブラウザで話せる画面のサーバー。自分のPCで動かして、Chromeで開いた画面をDiscordで画面共有する。

起動: python web_talk/server.py  →  Chrome で http://127.0.0.1:8000 を開く
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from fishaudio import FishAudio
from groq import Groq

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
# 性格・安全対策・文字数の上限は、リポジトリの prompts.py を使う
sys.path.insert(0, str(REPO))
from prompts import MAX_CHAT_CHARS, build_system_prompt, sanitize_reply, wrap_user_message  # noqa: E402

load_dotenv(REPO / ".env")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")
PORT = int(os.getenv("WEB_PORT", "8000"))

MAX_REPLY_TOKENS = 2048
HISTORY_LIMIT = 6  # 直近の発言を何件まで覚えるか
DAILY_TOKEN_BUDGET = 180_000  # Groq の無料枠（1日20万）より手前で止める
TTS_CHAR_LIMIT = 300  # 音声にする文字数の上限
JST = timezone(timedelta(hours=9))

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
fish_client = FishAudio(api_key=os.getenv("FISH_API_KEY"))

history: list[dict] = []
usage = {"day": "", "tokens": 0}


def today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def budget_left() -> bool:
    return usage["day"] != today() or usage["tokens"] < DAILY_TOKEN_BUDGET


def add_usage(used: int) -> None:
    if usage["day"] != today():
        usage["day"], usage["tokens"] = today(), 0
    usage["tokens"] += used


def cut_at_sentence(text: str, limit: int) -> str:
    """limit文字以内に収める。できるだけ文の区切り（。！？）で切る。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
    return head[: cut + 1] if cut > 0 else head


def json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(ROOT / "index.html")


async def avatar(request: web.Request) -> web.StreamResponse:
    path = ROOT / "avatar.png"
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def chat(request: web.Request) -> web.Response:
    data = await request.json()
    text = str(data.get("text", "")).strip()
    if not text:
        return json_error("入力が空です", 400)
    if len(text) > MAX_CHAT_CHARS:
        return json_error(f"{MAX_CHAT_CHARS}文字までです", 400)
    if not budget_left():
        return json_error("もうあなたとは話したくない", 429)

    history.append({"role": "user", "content": wrap_user_message(text, "相手")})
    del history[:-HISTORY_LIMIT]
    messages = [{"role": "system", "content": build_system_prompt()}, *history]
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            messages=messages,
            model=GROQ_MODEL,
            max_completion_tokens=MAX_REPLY_TOKENS,
        )
    except Exception as e:
        history.pop()
        return json_error(f"AIとの通信に失敗しました: {e}", 502)

    if completion.usage:
        add_usage(completion.usage.total_tokens)
    reply = sanitize_reply(completion.choices[0].message.content or "")
    history.append({"role": "assistant", "content": reply[:300]})
    del history[:-HISTORY_LIMIT]
    return web.json_response({"reply": reply})


async def tts(request: web.Request) -> web.Response:
    data = await request.json()
    text = cut_at_sentence(str(data.get("text", "")).strip(), TTS_CHAR_LIMIT)
    if not text:
        return json_error("読み上げる文字がありません", 400)
    try:
        audio = await asyncio.to_thread(
            fish_client.tts.convert,
            text=text,
            reference_id=FISH_REFERENCE_ID,
            format="mp3",
            model=FISH_MODEL,
        )
    except Exception as e:
        return json_error(f"音声の生成に失敗しました: {e}", 502)
    return web.Response(body=audio, content_type="audio/mpeg")


async def reset(request: web.Request) -> web.Response:
    history.clear()
    return web.json_response({"ok": True})


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/avatar.png", avatar)
app.router.add_post("/api/chat", chat)
app.router.add_post("/api/tts", tts)
app.router.add_post("/api/reset", reset)

if __name__ == "__main__":
    # 自分のPCからだけ見られるように 127.0.0.1 で待ち受ける（ネットワークには公開しない）
    web.run_app(app, host="127.0.0.1", port=PORT)
