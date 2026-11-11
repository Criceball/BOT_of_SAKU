"""ブラウザで話せる画面のサーバー。自分のPCで動かして、Chromeで開いた画面をDiscordで画面共有する。

起動: python web_talk/server.py  →  Chrome で http://127.0.0.1:8000 を開く
"""

import asyncio
import atexit
import hmac
from collections import defaultdict
import logging
import os
import re
import resource
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from fishaudio import AsyncFishAudio, FishAudio
from groq import Groq

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
# 性格・安全対策・文字数の上限は、リポジトリの prompts.py を使う
sys.path.insert(0, str(REPO))
from services import code_store, knowledge, usage_store  # noqa: E402
from prompts import (  # noqa: E402
    MAX_CHAT_CHARS,
    build_system_prompt,
    is_crazy,
    parse_emotions,
    sanitize_reply,
    strip_emotions,
    tts_prefix,
    tts_speed,
    web_persona,
    wrap_user_message,
)

load_dotenv(REPO / ".env")

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")
# Render などでは PORT が渡される。自分のPCで試すときは WEB_PORT（既定 8000）
PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT", "8000"))
# 誰でも使えないように、画面とAPIにアクセスキーを必須にする
ACCESS_KEY = os.getenv("WEB_ACCESS_KEY", "")  # 空のときは、画面を無効にする（main.py が判断する）
# アクセスキーが合ったとき、最初に読み込むパッケージ（Google ドライブの共有リンク。zip）
PACKAGE_URL = os.getenv("PACKAGE_URL", "")
# 開発者用のアクセスキー。これで入ると、回数の制限がかからない
DEV_ACCESS_KEY = os.getenv("WEB_DEV_ACCESS_KEY", "")
# 1人（1つのIPアドレス）が1日に話せる回数。開発者用のキーでは制限しない
WEB_DAILY_LIMIT = 30

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


# 回数の記録（利用者, 日付）→ 回数。利用者は、Render が渡す接続元のIPアドレス
web_usage: dict[tuple[str, str], int] = defaultdict(int)


def client_id(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or (request.remote or "unknown")


def remaining_for(request: web.Request) -> int | None:
    # 残りの回数。開発者用のキーなら None（無制限）
    if request.get("unlimited"):
        return None
    return max(0, WEB_DAILY_LIMIT - web_usage[(client_id(request), today())])


def count_use(request: web.Request) -> bool:
    # 1回分を数える。上限に達していたら False（数えない）
    if request.get("unlimited"):
        return True
    key = (client_id(request), today())
    if web_usage[key] >= WEB_DAILY_LIMIT:
        return False
    for old in [k for k in web_usage if k[1] != key[1]]:
        del web_usage[old]
    web_usage[key] += 1
    return True


@web.middleware
async def require_access_key(request: web.Request, handler):
    # /api/ への呼び出しは、アクセスキーが合っている場合だけ通す
    if request.path.startswith("/api/"):
        given = request.headers.get("X-Access-Key", "")
        if DEV_ACCESS_KEY and hmac.compare_digest(given, DEV_ACCESS_KEY):
            request["unlimited"] = True
        elif not (ACCESS_KEY and hmac.compare_digest(given, ACCESS_KEY)):
            return json_error("アクセスキーが違います", 401)
    return await handler(request)


async def healthz(request: web.Request) -> web.Response:
    # Render がこのサーバーの生存を確かめるための応答（応答しなければ、Render が再起動する）
    return web.json_response({"ok": True})


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
    if not count_use(request):
        return json_error(f"今日の利用回数（{WEB_DAILY_LIMIT}回）に達しました。明日また使えます。", 429)

    crazy = is_crazy(text)
    history.append({"role": "user", "content": wrap_user_message(text, "相手")})
    del history[:-HISTORY_LIMIT]
    messages = [{"role": "system", "content": build_system_prompt(web_persona(), extra=await knowledge.lookup(text))}, *history]
    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            messages=messages,
            model=GROQ_MODEL,
            max_completion_tokens=MAX_REPLY_TOKENS,
        )
    except Exception as e:
        history.pop()
        if not request.get("unlimited"):
            web_usage[(client_id(request), today())] -= 1   # 失敗した分は数えない
        return json_error(f"AIとの通信に失敗しました: {e}", 502)

    if completion.usage:
        add_usage(completion.usage.total_tokens)
        await usage_store.add(today(), completion.usage.total_tokens)   # 再起動しても消えないように保存
    raw = sanitize_reply(completion.choices[0].message.content or "", web_persona())
    reply = strip_emotions(raw)
    history.append({"role": "assistant", "content": reply[:300]})
    del history[:-HISTORY_LIMIT]
    return web.json_response({
        "reply": reply,
        "crazy": crazy,
        "sentences": parse_emotions(raw),
        "remaining": remaining_for(request),
    })


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
    # 再起動の後も、今日の使用量を続きから数えられるように、保存された値を読み直す
    usage["day"] = today()
    usage["tokens"] = (await usage_store.totals(today()))["tokens"]
    # 相づちの音声は、後ろで作る。終わるのを待つと、画面が開くまで時間がかかるため
    app["filler_task"] = asyncio.create_task(_make_filler())


def drive_download_url(link: str) -> str:
    # 共有リンク（/file/d/ID/view など）を、直接ダウンロードするリンクに変える
    m = re.search(r"/d/([\w-]+)", link) or re.search(r"[?&]id=([\w-]+)", link)
    if not m:
        return link
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


async def check(request: web.Request) -> web.Response:
    # 画面の参加前に、アクセスキーが合っているかを確かめる（合わなければ、ミドルウェアが 401 を返す）
    remaining = remaining_for(request)
    return web.json_response({"ok": True, "unlimited": remaining is None, "remaining": remaining})


async def package(request: web.Request) -> web.StreamResponse:
    # Google ドライブの zip を、サーバー経由で渡す（ブラウザから直接取ると、Google が通信を止めるため）
    if not PACKAGE_URL:
        raise web.HTTPNotFound()
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
    try:
        url = drive_download_url(PACKAGE_URL)
        upstream = await session.get(url)
        if "text/html" in upstream.headers.get("Content-Type", ""):
            # 大きいファイルは、Google が確認の画面を出す。確認の印を付けて、もう一度取りに行く
            page = await upstream.text()
            upstream.release()
            m = re.search(r"confirm=([0-9A-Za-z_-]+)", page)
            upstream = await session.get(f"{url}&confirm={m.group(1) if m else 't'}")
        if upstream.status != 200 or "text/html" in upstream.headers.get("Content-Type", ""):
            upstream.release()
            return json_error("Googleドライブからパッケージを取れませんでした（共有を「リンクを知っている全員」にしてください）", 502)
        response = web.StreamResponse(headers={"Content-Type": "application/zip"})
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        upstream.release()
        return response
    finally:
        await session.close()


async def reset(request: web.Request) -> web.Response:
    history.clear()
    return web.json_response({"ok": True})


# ===== 診断用：プログラムが止まった理由を、ログに残す =====
# 「終了の処理」が出ずに止まったら、強制終了（メモリ不足など）の可能性が高い。
def _diag(message: str) -> None:
    print(f"[診断] {message}", flush=True)


def _on_exit() -> None:
    _diag(f"プロセスが終了しました（pid={os.getpid()}）")


atexit.register(_on_exit)


def _on_loop_error(loop, context) -> None:
    _diag(f"予期しないエラー: {context.get('message')} {context.get('exception')!r}")


async def start_diagnostics(app: web.Application) -> None:
    _diag(f"起動しました（pid={os.getpid()}）")
    asyncio.get_running_loop().set_exception_handler(_on_loop_error)

    async def memory_report() -> None:
        # 5分ごとに、これまでのメモリの最大使用量（MB）を出す（Linux では KB 単位）
        while True:
            await asyncio.sleep(300)
            mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            _diag(f"メモリの最大使用量: {mb:.0f}MB")

    app["memory_report"] = asyncio.create_task(memory_report())


async def stop_diagnostics(app: web.Application) -> None:
    # SIGTERM（終了の依頼）や Ctrl+C で止まったとき、ここに来る
    _diag("終了の処理を始めました（SIGTERM など）")


class _SkipHealthzLog(logging.Filter):
    # Render のヘルスチェック（/healthz）の記録は、ログを見やすくするため省く
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


app = web.Application(middlewares=[require_access_key])
app.on_startup.append(start_diagnostics)
app.on_startup.append(load_filler)
app.on_cleanup.append(stop_diagnostics)
app.router.add_get("/", index)
app.router.add_get("/healthz", healthz)   # Render のヘルスチェック用（軽くて、鍵もいらない）
app.router.add_get("/avatar.png", avatar)
app.router.add_post("/api/chat", chat)
app.router.add_post("/api/tts", tts)
app.router.add_get("/api/filler", filler)
app.router.add_post("/api/check", check)
app.router.add_get("/api/package", package)
app.router.add_post("/api/reset", reset)
code_store.add_routes(app)  # /code で作ったページの表示（/view/<id>、/raw/<id>）

if __name__ == "__main__":
    if not ACCESS_KEY:
        raise SystemExit("WEB_ACCESS_KEY を設定してください（画面にアクセスするための合言葉です）")
    # Render などの外部サーバーでは 0.0.0.0 で待ち受ける。自分のPCで試すときは 127.0.0.1 にする
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    # アクセスの記録（いつ、どのリクエストが来て、何秒で応答したか）をログに出す
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    access_log = logging.getLogger("aiohttp.access")
    access_log.setLevel(logging.INFO)
    access_log.addFilter(_SkipHealthzLog())
    web.run_app(app, host=host, port=PORT, access_log=access_log)
