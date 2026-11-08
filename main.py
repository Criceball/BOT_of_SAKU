"""ボット（Discord）と話す画面（Web）を、1つのプロセスで同時に動かす。

Render の無料枠は、サービスごとに時間を数えるため、1つにまとめて24時間動かす（月744時間で収まる）。
- ボット：bot.py の bot（Discord と接続する）
- 画面：web_talk/server.py の app（WEB_ACCESS_KEY が無ければ無効。/healthz だけ応答する）
どちらかが止まっても、もう一方は動き続ける。
"""

import asyncio
import logging
import os
import sys

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot as discord_bot  # noqa: E402
from web_talk import server as web_server  # noqa: E402


class _SkipHealthzLog(logging.Filter):
    # Render のヘルスチェック（/healthz）の記録は省く（ログを見やすくする）
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def serve_http() -> web.AppRunner:
    if web_server.ACCESS_KEY:
        app = web_server.app
    else:
        print("WEB_ACCESS_KEY が未設定のため、画面は無効です（ボットだけ動かします）", flush=True)
        app = web.Application()
        app.router.add_get("/", healthz)
        app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    port = int(os.getenv("PORT") or os.getenv("WEB_PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[HTTP] ポート {port} で待ち受けます", flush=True)
    return runner


async def run_bot() -> None:
    try:
        await discord_bot.bot.start(discord_bot.DISCORD_TOKEN)
    except Exception as e:  # ボットが止まっても、画面は動かし続ける
        print(f"[ボット] 止まりました: {e!r}", flush=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    access_log = logging.getLogger("aiohttp.access")
    access_log.setLevel(logging.INFO)
    access_log.addFilter(_SkipHealthzLog())
    runner = await serve_http()
    asyncio.create_task(run_bot())
    try:
        await asyncio.Event().wait()  # 終わらないで待つ
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
