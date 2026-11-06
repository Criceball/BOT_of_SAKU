import asyncio
import os
import re
import uuid
from collections import defaultdict, deque

import discord
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq
from fishaudio import FishAudio

# 環境変数の読み込み
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FISH_API_KEY = os.getenv("FISH_API_KEY")
# Fish Audioで取得した音声モデルのID（.env に設定してください）
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")

GROQ_MODEL = "llama-3.3-70b-versatile"
SYSTEM_PROMPT = "あなたは親切な音声アシスタントです。自然な日本語で、少し短めに答えてください。"
# メンションでの会話で、チャンネルごとに保持するメッセージ数の上限
HISTORY_LIMIT = 10
# Discordの1メッセージあたりの文字数上限
DISCORD_MESSAGE_LIMIT = 2000

# 各クライアントの初期化
groq_client = Groq(api_key=GROQ_API_KEY)
fish_client = FishAudio(api_key=FISH_API_KEY)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# チャンネルごとの会話履歴（メンションでの会話用）
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
MENTION_PATTERN = re.compile(r"<@!?\d+>")


async def generate_reply(messages: list[dict]) -> str:
    # Groqの呼び出しは同期APIのため、別スレッドで実行してイベントループを止めない
    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        messages=messages,
        model=GROQ_MODEL,
    )
    return completion.choices[0].message.content


async def synthesize_speech(text: str) -> bytes:
    # 音声生成も同期APIのため、別スレッドで実行する
    return await asyncio.to_thread(
        fish_client.tts.convert,
        text=text,
        reference_id=FISH_REFERENCE_ID,
        format="mp3",
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


async def handle_mention(message: discord.Message) -> None:
    # メンション部分を取り除いて本文だけを取り出す
    text = MENTION_PATTERN.sub("", message.content).strip()
    if not text:
        await message.reply("何かお話しかけてください。")
        return

    history = chat_history[message.channel.id]
    # 複数人が話すチャンネルでも誰の発言か分かるように名前を付ける
    history.append({"role": "user", "content": f"{message.author.display_name}: {text}"})

    async with message.channel.typing():
        try:
            reply_text = await generate_reply([
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ])
        except Exception as e:
            history.pop()  # 失敗した発言は履歴に残さない
            await message.reply(f"⚠️ エラーが発生しました: `{e}`")
            return

    history.append({"role": "assistant", "content": reply_text})
    await message.reply(reply_text[:DISCORD_MESSAGE_LIMIT])


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

    async with ctx.typing():
        try:
            # ① Groq APIでAIの返答テキストを生成
            reply_text = await generate_reply([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ])

            # ② Fish Audio APIで音声を生成
            audio_bytes = await synthesize_speech(reply_text)

            # ③ ボイスチャンネルで再生（同時実行でも衝突しないように一意なファイル名を使う）
            voice_client = ctx.voice_client
            if not voice_client.is_playing():
                path = f"response_{uuid.uuid4().hex}.mp3"
                with open(path, "wb") as f:
                    f.write(audio_bytes)
                play_audio_file(voice_client, path)
                await ctx.send(f"🤖 **AIの返答**: {reply_text}")
            else:
                await ctx.send(f"🤖 (再生中): {reply_text}")

        except Exception as e:
            await ctx.send(f"⚠️ エラーが発生しました: `{e}`")


# ボットの起動
bot.run(DISCORD_TOKEN)
