import os
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

# 各クライアントの初期化
groq_client = Groq(api_key=GROQ_API_KEY)
fish_client = FishAudio(api_key=FISH_API_KEY)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ ログイン成功: {bot.user.name}")

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
            chat_completion = groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": "あなたは親切な音声アシスタントです。自然な日本語で、少し短めに答えてください。",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                model="llama-3.3-70b-versatile",
            )
            reply_text = chat_completion.choices[0].message.content

            # ② Fish Audio APIで音声を生成
            # ※ reference_id には、Fish Audioで取得した音声モデルのIDを指定してください
            audio_bytes = fish_client.tts.convert(
                text=reply_text,
                reference_id="あなたの使いたいボイスのreference_idを入力", 
                format="mp3"
            )

            # ③ 一時ファイルとして保存
            audio_filename = "response.mp3"
            with open(audio_filename, "wb") as f:
                f.write(audio_bytes)

            # ④ ボイスチャンネルで再生
            voice_client = ctx.voice_client
            if not voice_client.is_playing():
                source = discord.FFmpegPCMAudio(audio_filename)
                voice_client.play(source)
                await ctx.send(f"🤖 **AIの返答**: {reply_text}")
            else:
                await ctx.send(f"🤖 (再生中): {reply_text}")

        except Exception as e:
            await ctx.send(f"⚠️ エラーが発生しました: `{e}`")

# ボットの起動
bot.run(DISCORD_TOKEN)
