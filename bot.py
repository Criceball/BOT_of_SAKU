import asyncio
import io
import os
import re
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq
from fishaudio import FishAudio

from converter import (
    FORMAT_ALIASES,
    MAX_PDF_PAGES,
    UnsupportedConversion,
    convert_file,
    detect_target,
    normalize,
)
from prompts import (
    MAX_CHAT_CHARS,
    build_system_prompt,
    sanitize_reply,
    wrap_user_message,
)

# 環境変数の読み込み
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FISH_API_KEY = os.getenv("FISH_API_KEY")
# Fish Audioで取得した音声モデルのID（.env に設定してください）
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")
# 音声生成のモデル。既定は無料枠の s2.1-pro-free（有料に切り替えるなら s2.1-pro）
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")

# 会話に使うGroqのモデル。llama-3.3-70b-versatile は2026年8月に停止されたため、後継を既定にする
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
SYSTEM_PROMPT = build_system_prompt()
# メンションでの会話で、チャンネルごとに保持するメッセージ数の上限
HISTORY_LIMIT = 3
# 履歴の返答1件あたりの文字数上限（入力トークンを抑えて、Groqの1分・1日の上限を守るため）
HISTORY_CHAR_LIMIT = 300
# 返答の最大トークン数（長すぎる返答で上限を使い切らないため）
MAX_REPLY_TOKENS = 800
# Discordの1メッセージあたりの文字数上限
DISCORD_MESSAGE_LIMIT = 2000
# Discordの添付ファイルの上限（ブーストなしのサーバー）と、1メッセージの添付数上限
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10
# 1人1日の会話回数（メンションの会話と !talk の合計）。日付は日本時間で切り替える
DAILY_LIMIT_PER_USER = 20
JST = timezone(timedelta(hours=9))
QUOTA_MESSAGE = f"⚠️ 今日の利用上限（1人1日{DAILY_LIMIT_PER_USER}回）に達しました。明日また使えます。"

# 各クライアントの初期化
groq_client = Groq(api_key=GROQ_API_KEY)
fish_client = FishAudio(api_key=FISH_API_KEY)

# 必要なインテントだけを使う（Intents.all() は特権インテントを全部要求してしまうため）
# メッセージ本文の読み取り（メンション会話）に Message Content Intent が必要
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# チャンネルごとの会話履歴（メンションでの会話用）
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
MENTION_PATTERN = re.compile(r"<@!?\d+>")
# 利用回数の記録（ユーザーID, 日付）→ 回数。再起動するとリセットされる
daily_usage: dict[tuple[int, str], int] = defaultdict(int)


def take_quota(user_id: int) -> tuple[int, str] | None:
    """利用回数を1つ使う。上限に達していれば None を返す。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    # 前日以前の記録は捨てる
    for old_key in [k for k in daily_usage if k[1] != today]:
        del daily_usage[old_key]
    key = (user_id, today)
    if daily_usage[key] >= DAILY_LIMIT_PER_USER:
        return None
    daily_usage[key] += 1
    return key


def refund_quota(key: tuple[int, str] | None) -> None:
    # 失敗した場合は回数を戻す
    if key is not None and daily_usage[key] > 0:
        daily_usage[key] -= 1


async def generate_reply(messages: list[dict]) -> str:
    # Groqの呼び出しは同期APIのため、別スレッドで実行してイベントループを止めない
    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        messages=messages,
        model=GROQ_MODEL,
        max_completion_tokens=MAX_REPLY_TOKENS,
    )
    return completion.choices[0].message.content


async def synthesize_speech(text: str) -> bytes:
    # 音声生成も同期APIのため、別スレッドで実行する
    return await asyncio.to_thread(
        fish_client.tts.convert,
        text=text,
        reference_id=FISH_REFERENCE_ID,
        format="mp3",
        model=FISH_MODEL,
    )


def play_audio_file(voice_client: discord.VoiceClient, path: str) -> None:
    def cleanup(error: Exception | None) -> None:
        # 再生が終わったら一時ファイルを削除する
        try:
            os.remove(path)
        except OSError:
            pass
        if error:
            print(f"⚠️ 再生エラー: {error}")

    voice_client.play(discord.FFmpegPCMAudio(path), after=cleanup)


@bot.event
async def on_ready():
    print(f"✅ ログイン成功: {bot.user.name}")


@bot.event
async def on_message(message: discord.Message):
    # ボット自身や他のボットの発言には反応しない
    if message.author.bot:
        return

    # コマンド（!join など）以外で、ボットがメンションされたら会話する
    if (
        bot.user in message.mentions
        and not message.content.startswith(bot.command_prefix)
    ):
        await handle_mention(message)

    await bot.process_commands(message)


def get_attachments(message: discord.Message) -> list[discord.Attachment]:
    # 自分の添付がなければ、返信先のメッセージの添付を使う
    if message.attachments:
        return list(message.attachments)
    ref = message.reference.resolved if message.reference else None
    if isinstance(ref, discord.Message):
        return list(ref.attachments)
    return []


async def handle_conversion(message: discord.Message, attachment: discord.Attachment, text: str) -> None:
    src = normalize(os.path.splitext(attachment.filename)[1])
    if not src:
        await message.reply("❌ このファイル形式は変換に対応していません。")
        return

    dst = detect_target(text, src)
    if dst is None:
        supported = ", ".join(sorted(FORMAT_ALIASES))
        await message.reply(f"変換先の形式を指定してください（例: 「これをjpegにして」）。\n対応形式: {supported}")
        return

    if attachment.size > MAX_FILE_BYTES:
        await message.reply("❌ ファイルが大きすぎます（25MBまで）。")
        return

    async with message.channel.typing():
        try:
            data = await attachment.read()
            outputs = await asyncio.to_thread(convert_file, data, src, dst)
        except UnsupportedConversion as e:
            await message.reply(f"❌ {e}")
            return
        except Exception as e:
            await message.reply(f"⚠️ 変換に失敗しました: `{e}`")
            return

    if any(len(b) > MAX_FILE_BYTES for _, b in outputs):
        await message.reply("❌ 変換後のファイルが大きすぎて送信できません。")
        return

    files = [discord.File(io.BytesIO(b), filename=n) for n, b in outputs[:MAX_UPLOAD_FILES]]
    note = "（先頭ページのみ）" if src == "pdf" and dst != "txt" and len(outputs) == MAX_PDF_PAGES else ""
    await message.reply(f"✅ {src} → {dst} に変換しました。{note}", files=files)


async def handle_mention(message: discord.Message) -> None:
    # メンション部分を取り除いて本文だけを取り出す
    text = MENTION_PATTERN.sub("", message.content).strip()

    # 添付ファイルがあれば、会話ではなくファイル変換として扱う
    attachments = get_attachments(message)
    if attachments:
        await handle_conversion(message, attachments[0], text)
        return

    if not text:
        await message.reply("何かお話しかけてください。")
        return

    if len(text) > MAX_CHAT_CHARS:
        await message.reply(f"⚠️ 長すぎます（{MAX_CHAT_CHARS}文字までです）。")
        return

    quota_key = take_quota(message.author.id)
    if quota_key is None:
        await message.reply(QUOTA_MESSAGE)
        return

    history = chat_history[message.channel.id]
    # 誰の発言か分かるように名前を付け、タグで囲んでデータとして渡す
    history.append({
        "role": "user",
        "content": wrap_user_message(text, message.author.display_name),
    })

    async with message.channel.typing():
        try:
            reply_text = await generate_reply([
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ])
        except Exception as e:
            history.pop()  # 失敗した発言は履歴に残さない
            refund_quota(quota_key)
            if "429" in str(e):
                # 無料枠の上限（1分・1日）に当たった場合は、分かりやすく案内する
                await message.reply("⚠️ 今は混み合っています。少し待ってから、もう一度話しかけてください。")
            else:
                await message.reply(f"⚠️ エラーが発生しました: `{e}`")
            return

    reply_text = sanitize_reply(reply_text)
    history.append({"role": "assistant", "content": reply_text[:HISTORY_CHAR_LIMIT]})
    # AIの返答で @everyone などが鳴らないように、メンションを無効にする
    await message.reply(
        reply_text[:DISCORD_MESSAGE_LIMIT],
        allowed_mentions=discord.AllowedMentions.none(),
    )


# 1. ボイスチャンネルに参加
@bot.command(name="join")
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
        await ctx.send(f"🔊 `{channel.name}` に参加しました！")
    else:
        await ctx.send("❌ 先にボイスチャンネルに入っていてください。")


# 2. ボイスチャンネルから退出
@bot.command(name="leave")
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 ボイスチャンネルから退出しました。")
    else:
        await ctx.send("❌ ボットはボイスチャンネルにいません。")


# 3. GroqとFish Audioを使った音声対話
@bot.command(name="talk")
async def talk(ctx, *, prompt: str):
    if not ctx.voice_client:
        await ctx.send("❌ 先に `!join` でボットを通話に入れてください。")
        return

    quota_key = take_quota(ctx.author.id)
    if quota_key is None:
        await ctx.send(QUOTA_MESSAGE)
        return

    async with ctx.typing():
        try:
            # ① Groq APIでAIの返答テキストを生成
            reply_text = sanitize_reply(await generate_reply([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": wrap_user_message(prompt, ctx.author.display_name)},
            ]))

            # ② Fish Audio APIで音声を生成
            audio_bytes = await synthesize_speech(reply_text)

            # ③ ボイスチャンネルで再生（同時実行でも衝突しないように一意なファイル名を使う）
            voice_client = ctx.voice_client
            if not voice_client.is_playing():
                path = f"response_{uuid.uuid4().hex}.mp3"
                with open(path, "wb") as f:
                    f.write(audio_bytes)
                play_audio_file(voice_client, path)
                await ctx.send(f"🤖 **AIの返答**: {reply_text}", allowed_mentions=discord.AllowedMentions.none())
            else:
                await ctx.send(f"🤖 (再生中): {reply_text}", allowed_mentions=discord.AllowedMentions.none())

        except Exception as e:
            refund_quota(quota_key)
            await ctx.send(f"⚠️ エラーが発生しました: `{e}`")


async def start_health_server() -> None:
    # Koyeb などのWebサービスは、PORT で HTTP に応答しないと「異常」と判定される。
    # PORT が設定されているときだけ、ヘルスチェック用の小さなサーバーを立てる。
    port = os.getenv("PORT")
    if not port:
        return

    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


bot.setup_hook = start_health_server

# ボットの起動
bot.run(DISCORD_TOKEN)
