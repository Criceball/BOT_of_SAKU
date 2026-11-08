"""ブラウザで話せる画面のサーバー。自分のPCで動かして、Chromeで開いた画面をDiscordで画面共有する。

起動: python web_talk/server.py  →  Chrome で http://127.0.0.1:8000 を開く
"""

import asyncio
import hmac
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from fishaudio import AsyncFishAudio, FishAudio
from groq import Groq

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
# 性格・安全対策・文字数の上限は、リポジトリの prompts.py を使う
sys.path.insert(0, str(REPO))
from prompts import (  # noqa: E402
    MAX_CHAT_CHARS,
    build_system_prompt,
    is_crazy,
    parse_emotions,
    sanitize_reply,
    strip_emotions,
    tts_prefix,
    tts_speed,
    wrap_user_message,
)

load_dotenv(REPO / ".env")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")
# Render などでは PORT が渡される。自分のPCで試すときは WEB_PORT（既定 8000）
PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT", "8000"))
# 誰でも使えないように、画面とAPIにアクセスキーを必須にする
ACCESS_KEY = os.getenv("WEB_ACCESS_KEY", "")
if not ACCESS_KEY:
    raise SystemExit("WEB_ACCESS_KEY を設定してください（画面にアクセスするための合言葉です）")

MAX_REPLY_TOKENS = 2048
HISTORY_LIMIT = 6  # 直近の発言を何件まで覚えるか
DAILY_TOKEN_BUDGET = 180_000  # Groq の無料枠（1日20万）より手前で止める
TTS_CHAR_LIMIT = 300  # 音声にする文字数の上限
JST = timezone(timedelta(hours=9))

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
fish_client = FishAudio(api_key=os.getenv("FISH_API_KEY"))
fish_async = AsyncFishAudio(api_key=os.getenv("FISH_API_KEY"))

# 返事を作っている間に流す相づち（起動時に一度だけ音声を作って、使い回す）
FILLER_TEXT = "はい！"
filler_audio: dict = {}

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


@web.middleware
async def require_access_key(request: web.Request, handler):
    # /api/ への呼び出しは、アクセスキーが合っている場合だけ通す
    if request.path.startswith("/api/"):
        given = request.headers.get("X-Access-Key", "")
        if not hmac.compare_digest(given, ACCESS_KEY):
            return json_error("アクセスキーが違います", 401)
    return await handler(request)


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

    crazy = is_crazy(text)
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
    raw = sanitize_reply(completion.choices[0].message.content or "")
    reply = strip_emotions(raw)
    history.append({"role": "assistant", "content": reply[:300]})
    del history[:-HISTORY_LIMIT]
    return web.json_response({"reply": reply, "crazy": crazy, "sentences": parse_emotions(raw)})


async def tts(request: web.Request) -> web.StreamResponse:
    # 音声は全部作り終わるのを待たず、できた部分から順に返す（再生を早く始めるため）
    data = await request.json()
    text = cut_at_sentence(str(data.get("text", "")).strip(), TTS_CHAR_LIMIT)
    if not text:
        return json_error("読み上げる文字がありません", 400)
    # 感情のタグ（角かっこ）を付けて、声の調子を変える
    crazy = bool(data.get("crazy"))
    text = tts_prefix(crazy, data.get("emotion")) + text
    try:
        stream = await fish_async.tts.stream(
            text=text,
            reference_id=FISH_REFERENCE_ID,
            format="mp3",
            model=FISH_MODEL,
            speed=tts_speed(crazy),
        )
    except Exception as e:
        return json_error(f"音声の生成に失敗しました: {e}", 502)

    response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
    await response.prepare(request)
    async for chunk in stream:
        await response.write(chunk)
    await response.write_eof()
    return response


async def filler(request: web.Request) -> web.Response:
    if not filler_audio.get("data"):
        raise web.HTTPNotFound()
    return web.Response(body=filler_audio["data"], content_type="audio/mpeg")


async def _make_filler() -> None:
    try:
        # Fish の応答が遅くても止まらないように、時間の上限を付ける
        filler_audio["data"] = await asyncio.wait_for(
            asyncio.to_thread(
                fish_client.tts.convert,
                text=FILLER_TEXT,
                reference_id=FISH_REFERENCE_ID,
                format="mp3",
                model=FISH_MODEL,
            ),
            timeout=30,
        )
    except Exception as e:
        print(f"相づちの音声を作れませんでした: {e}")


async def load_filler(app: web.Application) -> None:
    # 相づちの音声は、後ろで作る。終わるのを待つと、画面が開くまで時間がかかるため
    app["filler_task"] = asyncio.create_task(_make_filler())


async def reset(request: web.Request) -> web.Response:
    history.clear()
    return web.json_response({"ok": True})


app = web.Application(middlewares=[require_access_key])
app.on_startup.append(load_filler)
app.router.add_get("/", index)
app.router.add_get("/avatar.png", avatar)
app.router.add_post("/api/chat", chat)
app.router.add_post("/api/tts", tts)
app.router.add_get("/api/filler", filler)
app.router.add_post("/api/reset", reset)

if __name__ == "__main__":
    # Render などの外部サーバーでは 0.0.0.0 で待ち受ける。自分のPCで試すときは 127.0.0.1 にする
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    web.run_app(app, host=host, port=PORT)
