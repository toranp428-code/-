import os
import random
import sqlite3
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "penguin_bot.sqlite3")
SLOT_COST = 500
GACHA_COST = 300
DEFAULT_STARTING_PEN = 1000

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

db_lock = asyncio.Lock()


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS elections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            candidates TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS votes (
            election_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            candidate TEXT NOT NULL,
            PRIMARY KEY (election_id, voter_id),
            FOREIGN KEY (election_id) REFERENCES elections(id)
        );
        CREATE TABLE IF NOT EXISTS gacha_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        count = con.execute("SELECT COUNT(*) FROM gacha_items").fetchone()[0]
        if count == 0:
            con.executemany(
                "INSERT INTO gacha_items(name, weight, enabled) VALUES (?, ?, 1)",
                [
                    ("ノーマル賞", 60),
                    ("レア賞", 25),
                    ("スーパーレア賞", 10),
                    ("超激レア賞", 4),
                    ("伝説賞", 1),
                ],
            )


def get_balance(user_id: int) -> int:
    with connect_db() as con:
        row = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance"]) if row else 0


def ensure_wallet(user_id: int):
    with connect_db() as con:
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES (?, 0)", (user_id,))


def change_balance(user_id: int, amount: int, reason: str) -> tuple[bool, int]:
    """Atomically adjust a user's global balance. Negative adjustments cannot overdraw."""
    with connect_db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES (?, 0)", (user_id,))
        row = con.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
        current = int(row["balance"])
        if current + amount < 0:
            con.rollback()
            return False, current
        new_balance = current + amount
        con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new_balance, user_id))
        con.execute(
            "INSERT INTO ledger(user_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, reason[:200], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        return True, new_balance


def admin_check(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.user.guild_permissions.administrator)


def parse_candidates(raw: str) -> list[str]:
    return [x.strip()[:80] for x in raw.split(",") if x.strip()][:20]


class MainPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ペンの残高", style=discord.ButtonStyle.primary, emoji="🪙", custom_id="penguin:balance")
    async def balance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"🪙 **所持ペン：{bal:,}ペン**\n※ペンは全サーバー共通だよ",
            ephemeral=True,
        )

    @discord.ui.button(label="スロット（500ペン）", style=discord.ButtonStyle.success, emoji="🎰", custom_id="penguin:slot")
    async def slot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, balance = change_balance(interaction.user.id, -SLOT_COST, "スロット参加費")
        if not ok:
            await interaction.response.send_message(
                f"ペンが足りないよ！必要：{SLOT_COST:,}ペン / 所持：{balance:,}ペン",
                ephemeral=True,
            )
            return
        symbols = ["🐧", "🍙", "⭐", "💎", "🪙", "🍀"]
        result = [random.choice(symbols) for _ in range(3)]
        payout = 0
        if result[0] == result[1] == result[2]:
            payout = {
                "🐧": 3000, "🍙": 2000, "⭐": 1500,
                "💎": 2500, "🪙": 1200, "🍀": 1800
            }[result[0]]
        elif len(set(result)) == 2:
            payout = 200
        if payout:
            _, balance = change_balance(interaction.user.id, payout, "スロット配当")
        net = payout - SLOT_COST
        outcome = "当たり！" if payout else "今回はハズレ…"
        await interaction.response.send_message(
            f"🎰 **スロット結果**\n\n"
            f"╭─────────╮\n　 {'　'.join(result)}\n╰─────────╯\n"
            f"{outcome}\n参加費：-{SLOT_COST:,}ペン\n配当：+{payout:,}ペン\n"
            f"差し引き：{net:+,}ペン\n現在の残高：{balance:,}ペン",
            ephemeral=True,
        )

    @discord.ui.button(label="ガチャ（300ペン）", style=discord.ButtonStyle.primary, emoji="🎁", custom_id="penguin:gacha")
    async def gacha_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, balance = change_balance(interaction.user.id, -GACHA_COST, "ガチャ参加費")
        if not ok:
            await interaction.response.send_message(
                f"ペンが足りないよ！必要：{GACHA_COST:,}ペン / 所持：{balance:,}ペン",
                ephemeral=True,
            )
            return
        with connect_db() as con:
            items = con.execute(
                "SELECT name, weight FROM gacha_items WHERE enabled=1 AND weight>0"
            ).fetchall()
        if not items:
            _, balance = change_balance(interaction.user.id, GACHA_COST, "ガチャ景品なしの返金")
            await interaction.response.send_message("ガチャ景品がまだ設定されていないため返金したよ", ephemeral=True)
            return
        prize = random.choices([r["name"] for r in items], weights=[r["weight"] for r in items], k=1)[0]
        await interaction.response.send_message(
            f"🎁 **ガチャ結果！**\n\n獲得景品：**{discord.utils.escape_markdown(prize)}**\n"
            f"消費：{GACHA_COST:,}ペン\n残高：{balance:,}ペン\n\n"
            f"※景品は現在、Bot内の獲得結果として記録・表示されるよ",
            ephemeral=True,
        )

    @discord.ui.button(label="選挙一覧", style=discord.ButtonStyle.secondary, emoji="🗳️", custom_id="penguin:elections")
    async def election_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってね", ephemeral=True)
            return
        with connect_db() as con:
            rows = con.execute(
                "SELECT id, title, status FROM elections WHERE guild_id=? ORDER BY id DESC LIMIT 10",
                (interaction.guild.id,),
            ).fetchall()
        if not rows:
            await interaction.response.send_message("現在、選挙はありません", ephemeral=True)
            return
        view = ElectionListView([dict(r) for r in rows])
        lines = [f"**#{r['id']}** {discord.utils.escape_markdown(r['title'])} — {'投票中' if r['status']=='open' else '終了'}" for r in rows]
        await interaction.response.send_message("🗳️ **選挙一覧**\n" + "\n".join(lines) + "\n下のボタンから投票できるよ", view=view, ephemeral=True)


class ElectionListView(discord.ui.View):
    def __init__(self, elections):
        super().__init__(timeout=180)
        for election in elections[:5]:
            label = f"#{election['id']} {election['title']}"[:80]
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if election["status"] == "open" else discord.ButtonStyle.secondary,
                disabled=election["status"] != "open",
            )
            async def callback(interaction: discord.Interaction, election_id=election["id"]):
                await show_vote(interaction, election_id)
            button.callback = callback
            self.add_item(button)


async def show_vote(interaction: discord.Interaction, election_id: int):
    with connect_db() as con:
        election = con.execute(
            "SELECT * FROM elections WHERE id=? AND guild_id=? AND status='open'",
            (election_id, interaction.guild.id if interaction.guild else -1),
        ).fetchone()
        if not election:
            await interaction.response.send_message("この選挙は終了したか、見つからないよ", ephemeral=True)
            return
        candidates = election["candidates"].split("\n")
    view = VoteView(election_id, candidates)
    await interaction.response.send_message(
        f"🗳️ **{discord.utils.escape_markdown(election['title'])}**\n候補者を1人選んでね\n投票先は公開されないよ",
        view=view,
        ephemeral=True,
    )


class VoteSelect(discord.ui.Select):
    def __init__(self, election_id: int, candidates: list[str]):
        self.election_id = election_id
        options = [discord.SelectOption(label=c[:100], value=c) for c in candidates[:25]]
        super().__init__(placeholder="投票する候補者を選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        with connect_db() as con:
            election = con.execute(
                "SELECT status FROM elections WHERE id=?", (self.election_id,)
            ).fetchone()
            if not election or election["status"] != "open":
                await interaction.response.send_message("この選挙は終了しているよ", ephemeral=True)
                return
            try:
                con.execute(
                    "INSERT INTO votes(election_id, voter_id, candidate) VALUES (?, ?, ?)",
                    (self.election_id, interaction.user.id, self.values[0]),
                )
                con.commit()
            except sqlite3.IntegrityError:
                await interaction.response.send_message("この選挙にはもう投票済みだよ", ephemeral=True)
                return
        await interaction.response.send_message("投票を受け付けたよ！投票先は非公開だよ", ephemeral=True)


class VoteView(discord.ui.View):
    def __init__(self, election_id: int, candidates: list[str]):
        super().__init__(timeout=180)
        self.add_item(VoteSelect(election_id, candidates))


class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ペンを付与", style=discord.ButtonStyle.success, emoji="➕", custom_id="penguin:admin_add")
    async def add_pen(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        await interaction.response.send_modal(PenModal())

    @discord.ui.button(label="選挙を作成", style=discord.ButtonStyle.primary, emoji="🗳️", custom_id="penguin:admin_election")
    async def create_election(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        await interaction.response.send_modal(ElectionModal())

    @discord.ui.button(label="選挙を締め切る", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="penguin:admin_close_election")
    async def close_election(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        with connect_db() as con:
            row = con.execute(
                "SELECT id, title FROM elections WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                (interaction.guild.id,),
            ).fetchone()
            if not row:
                await interaction.response.send_message("投票中の選挙がないよ", ephemeral=True)
                return
            con.execute("UPDATE elections SET status='closed' WHERE id=?", (row["id"],))
            results = con.execute(
                "SELECT candidate, COUNT(*) AS votes FROM votes WHERE election_id=? GROUP BY candidate ORDER BY votes DESC",
                (row["id"],),
            ).fetchall()
            con.commit()
        total = sum(r["votes"] for r in results)
        if results:
            high = max(r["votes"] for r in results)
            winners = [r["candidate"] for r in results if r["votes"] == high]
            winner_text = "、".join(winners) if len(winners) > 1 else winners[0]
            lines = "\n".join(f"• {discord.utils.escape_markdown(r['candidate'])}：{r['votes']}票" for r in results)
            result_text = f"**結果**\n{lines}\n\n当選：**{discord.utils.escape_markdown(winner_text)}**"
        else:
            result_text = "投票がなかったため、結果はありません"
        await interaction.response.send_message(
            f"🔒 **選挙を締め切ったよ**\n{discord.utils.escape_markdown(row['title'])}\n総投票数：{total}\n{result_text}",
            ephemeral=False,
        )

    @discord.ui.button(label="ガチャ景品を追加", style=discord.ButtonStyle.secondary, emoji="🎁", custom_id="penguin:admin_gacha_item")
    async def add_gacha_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        await interaction.response.send_modal(GachaItemModal())


class PenModal(discord.ui.Modal, title="ペンを付与"):
    user_id = discord.ui.TextInput(label="ユーザーID", placeholder="DiscordユーザーID", max_length=22)
    amount = discord.ui.TextInput(label="付与するペン", placeholder="例：1000", max_length=12)

    async def on_submit(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        try:
            uid = int(str(self.user_id.value).strip())
            amount = int(str(self.amount.value).strip())
            if amount <= 0 or amount > 1_000_000_000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("ユーザーIDとペン数を正しく入力してね", ephemeral=True)
            return
        ok, balance = change_balance(uid, amount, f"管理者付与 by {interaction.user.id}")
        await interaction.response.send_message(f"<@{uid}> に {amount:,}ペンを付与したよ\n新しい残高：{balance:,}ペン", ephemeral=True)


class ElectionModal(discord.ui.Modal, title="モデレーター選挙を作成"):
    title_input = discord.ui.TextInput(label="選挙タイトル", placeholder="例：10月モデレーター選挙", max_length=80)
    candidates_input = discord.ui.TextInput(
        label="候補者名（カンマ区切り）",
        placeholder="例：Aさん, Bさん, Cさん",
        style=discord.TextStyle.paragraph,
        max_length=900,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not admin_check(interaction) or not interaction.guild:
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        candidates = parse_candidates(str(self.candidates_input.value))
        if len(candidates) < 2:
            await interaction.response.send_message("候補者を2人以上、カンマ区切りで入力してね", ephemeral=True)
            return
        if len(candidates) > 25:
            await interaction.response.send_message("候補者は25人までだよ", ephemeral=True)
            return
        with connect_db() as con:
            cur = con.execute(
                "INSERT INTO elections(guild_id, title, candidates, status, created_by, created_at) VALUES (?, ?, ?, 'open', ?, ?)",
                (interaction.guild.id, str(self.title_input.value).strip(), "\n".join(candidates),
                 interaction.user.id, datetime.now(timezone.utc).isoformat()),
            )
            election_id = cur.lastrowid
            con.commit()
        await interaction.response.send_message(
            f"🗳️ **選挙を作成したよ！**\nID：#{election_id}\nタイトル：{discord.utils.escape_markdown(str(self.title_input.value))}\n"
            f"候補者：{', '.join(candidates)}\nメインパネルの「選挙一覧」から投票できるよ",
            ephemeral=True,
        )


class GachaItemModal(discord.ui.Modal, title="ガチャ景品を追加"):
    name = discord.ui.TextInput(label="景品名", placeholder="例：ペンギン称号", max_length=80)
    weight = discord.ui.TextInput(label="排出ウェイト（大きいほど出やすい）", placeholder="例：10", max_length=8)

    async def on_submit(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("サーバー管理者だけが使えるよ", ephemeral=True)
            return
        try:
            weight = int(str(self.weight.value).strip())
            if weight < 1 or weight > 1_000_000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("ウェイトは1〜1000000の整数にしてね", ephemeral=True)
            return
        with connect_db() as con:
            con.execute("INSERT INTO gacha_items(name, weight, enabled) VALUES (?, ?, 1)", (str(self.name.value).strip(), weight))
            con.commit()
        await interaction.response.send_message(f"景品「{discord.utils.escape_markdown(str(self.name.value))}」をウェイト{weight}で追加したよ", ephemeral=True)


@bot.event
async def on_ready():
    print(f"ログイン成功：{bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="panel", description="ペンギンBotのメインパネルを表示")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🐧 ペンギンBot",
        description=(
            "ボタンから機能を使ってね！\n\n"
            "🪙 **ペンの残高**：共通通貨の確認\n"
            "🎰 **スロット**：1回500ペン\n"
            "🎁 **ガチャ**：1回300ペン\n"
            "🗳️ **選挙一覧**：匿名で投票\n\n"
            "ペンは全Discordサーバーで共通だよ"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=MainPanel())


@bot.tree.command(name="adminpanel", description="管理者用パネルを表示")
@app_commands.check(admin_check)
async def adminpanel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚙️ ペンギンBot 管理パネル",
        description="管理者向け操作を選んでね\n\nペン付与・選挙作成/締め切り・ガチャ景品追加",
        color=discord.Color.dark_grey(),
    )
    await interaction.response.send_message(embed=embed, view=AdminPanel(), ephemeral=True)


@bot.tree.command(name="balance", description="自分のペン残高を確認")
async def balance_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 所持ペン：**{get_balance(interaction.user.id):,}ペン**", ephemeral=True)


@bot.tree.command(name="givepen", description="管理者がペンを付与（管理パネル推奨）")
@app_commands.describe(user="付与する相手", amount="付与するペン数")
@app_commands.check(admin_check)
async def givepen(interaction: discord.Interaction, user: discord.User, amount: app_commands.Range[int, 1, 1_000_000_000]):
    _, bal = change_balance(user.id, amount, f"管理者付与 by {interaction.user.id}")
    await interaction.response.send_message(f"{user.mention} に {amount:,}ペン付与したよ（残高 {bal:,}ペン）", ephemeral=True)


@bot.tree.command(name="gacha_odds", description="ガチャの排出確率一覧を確認")
async def gacha_odds(interaction: discord.Interaction):
    with connect_db() as con:
        rows = con.execute("SELECT name, weight FROM gacha_items WHERE enabled=1 AND weight>0 ORDER BY weight DESC").fetchall()
    total = sum(r["weight"] for r in rows)
    if not total:
        await interaction.response.send_message("ガチャ景品がないよ", ephemeral=True)
        return
    lines = [f"• {discord.utils.escape_markdown(r['name'])}：{r['weight']/total*100:.2f}%" for r in rows]
    await interaction.response.send_message("🎁 **ガチャ排出確率**\n" + "\n".join(lines), ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "この操作はサーバー管理者だけが使えるよ"
    else:
        message = "エラーが発生したよ、少し待ってからもう一度試してね"
        print(f"Slash command error: {error!r}")
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class HealthHandler(BaseHTTPRequestHandler):
    """Small HTTP endpoint for Render Web Service health checks."""
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        body = b"Penguin Discord Bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Keep routine health checks from filling Render logs
        return


def start_http_server():
    # Render provides PORT. Bind to 0.0.0.0 so Render can reach the service.
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-http", daemon=True)
    thread.start()
    print(f"HTTPサーバー起動：0.0.0.0:{port} ( / と /health )")
    return server


async def main():
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません")
    init_db()
    async with bot:
        bot.add_view(MainPanel())
        bot.add_view(AdminPanel())
        await bot.start(TOKEN)


if __name__ == "__main__":
    start_http_server()
    asyncio.run(main())
