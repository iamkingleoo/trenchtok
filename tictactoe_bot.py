#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║        🎮  TIC TAC TOE  TELEGRAM  BOT  🎮           ║
║  ─────────────────────────────────────────────────  ║
║  • /duel @user in GROUP — no DM needed!             ║
║  • Board shown directly in group chat               ║
║  • Win streak, leaderboard, stats, rematch          ║
║  • 60-second inactivity timeout (forfeit)           ║
╚══════════════════════════════════════════════════════╝

Install:
    pip install "python-telegram-bot[job-queue]==20.7"

Run:
    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    python tictactoe_bot.py
"""

import os
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN           = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH         = "tictactoe.db"
TIMEOUT_SECONDS = 60
DUEL_EXPIRE_SEC = 60
CHECK_INTERVAL  = 10
PTS_WIN         = 10
PTS_DRAW        = 3

E_EMPTY = "⬜"
E_X     = "❌"
E_O     = "⭕"

logging.basicConfig(format="%(asctime)s | %(levelname)-8s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE
# ═══════════════════════════════════════════════════════════════════════════════

active_games:      dict = {}   # game_id  → game dict
user_to_game:      dict = {}   # user_id  → game_id
pending_duels:     dict = {}   # duel_id  → duel dict
matchmaking_queue: dict = {}   # user_id  → chat_id (DM matchmaking)

_gc = 0
_dc = 0

def _gid():
    global _gc; _gc += 1; return _gc

def _did():
    global _dc; _dc += 1; return _dc

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def _db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    NOT NULL DEFAULT 'Unknown',
                wins        INTEGER NOT NULL DEFAULT 0,
                losses      INTEGER NOT NULL DEFAULT 0,
                draws       INTEGER NOT NULL DEFAULT 0,
                points      INTEGER NOT NULL DEFAULT 0,
                win_streak  INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.commit()

def upsert_user(uid: int, name: str):
    with _db() as c:
        c.execute("""
            INSERT INTO users (user_id, username) VALUES (?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
        """, (uid, name or "Unknown"))
        c.commit()

def fetch_user(uid: int):
    with _db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def db_win(uid):
    with _db() as c:
        c.execute("""
            UPDATE users SET wins=wins+1, points=points+?,
                win_streak=win_streak+1, best_streak=MAX(best_streak, win_streak+1)
            WHERE user_id=?
        """, (PTS_WIN, uid))
        c.commit()

def db_loss(uid):
    with _db() as c:
        c.execute("UPDATE users SET losses=losses+1, win_streak=0 WHERE user_id=?", (uid,))
        c.commit()

def db_draw(uid):
    with _db() as c:
        c.execute("UPDATE users SET draws=draws+1, points=points+?, win_streak=0 WHERE user_id=?", (PTS_DRAW, uid))
        c.commit()

def fetch_top10():
    with _db() as c:
        return c.execute("""
            SELECT username, wins, losses, draws, points, best_streak
            FROM users ORDER BY points DESC, wins DESC LIMIT 10
        """).fetchall()

# ═══════════════════════════════════════════════════════════════════════════════
# BOARD LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

WIN_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

def check_result(board):
    for a,b,c in WIN_LINES:
        if board[a] and board[a]==board[b]==board[c]:
            return board[a]
    return "draw" if all(board) else None

def sym(s):   return E_X if s=="X" else E_O
def other(s): return "O" if s=="X" else "X"

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════

def board_kb(board, game_id):
    m = {"X": E_X, "O": E_O, "": E_EMPTY}
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r*3+c
            row.append(InlineKeyboardButton(m[board[i]], callback_data=f"mv:{game_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def after_kb(game_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Play Again", callback_data=f"rematch:{game_id}"),
        InlineKeyboardButton("📊 Stats",      callback_data="stats"),
        InlineKeyboardButton("🏆 Top 10",     callback_data="leaderboard"),
    ]])

def duel_kb(duel_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"da:{duel_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"dd:{duel_id}"),
    ]])

def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Play Game",   callback_data="play")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
         InlineKeyboardButton("📊 My Stats",    callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Help",        callback_data="help")],
    ])

# ═══════════════════════════════════════════════════════════════════════════════
# TEXT FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════

def board_text(game, status=""):
    xn  = game["names"][game["players"]["x"]]
    on  = game["names"][game["players"]["o"]]
    tid = game["players"][game["turn"].lower()]
    ts  = sym(game["turn"])
    tn  = game["names"][tid]
    head = f"🎮 *Tic Tac Toe*\n{E_X} *{xn}*  vs  {E_O} *{on}*\n{'─'*26}\n"
    body = status if status else f"🕹 {ts} *{tn}*'s turn"
    return head + body

def stats_text(row, name):
    total = row["wins"]+row["losses"]+row["draws"]
    wr    = f"{row['wins']/total*100:.1f}%" if total else "0%"
    return (
        f"📊 *Stats — {name}*\n{'─'*24}\n"
        f"🏆 Wins          *{row['wins']}*\n"
        f"❌ Losses        *{row['losses']}*\n"
        f"🤝 Draws         *{row['draws']}*\n"
        f"🎮 Games         *{total}*\n"
        f"📈 Win rate      *{wr}*\n{'─'*24}\n"
        f"⭐ Points        *{row['points']}*\n"
        f"🔥 Streak        *{row['win_streak']}*\n"
        f"🏅 Best streak   *{row['best_streak']}*"
    )

def lb_text(rows):
    if not rows: return "_No players yet!_"
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = ["🏆 *Global Leaderboard — Top 10*", "─"*30]
    for i, r in enumerate(rows):
        lines.append(
            f"{medals[i]} *{r['username']}*  ⭐{r['points']}pts  "
            f"✅{r['wins']}W ❌{r['losses']}L 🤝{r['draws']}D  🔥{r['best_streak']}"
        )
    return "\n".join(lines)

HELP_MSG = (
    "ℹ️ *How to Play*\n\n"
    "*Group Chat (recommended):*\n"
    "  `/duel @username` — challenge anyone\n"
    "  Board appears right here — no DM needed!\n\n"
    "*Private Chat (random match):*\n"
    "  Tap 🎮 Play Game to enter queue\n\n"
    "📋 *Rules*\n"
    "  Tap a square to place your mark\n"
    "  3 in a row = win!\n"
    "  No move in 60s = forfeit\n\n"
    f"⭐ Win +{PTS_WIN}pts  Draw +{PTS_DRAW}pts  Loss +0pts\n\n"
    "⚡ /duel @user  /quit  /stats  /leaderboard"
)

# ═══════════════════════════════════════════════════════════════════════════════
# GAME CORE
# ═══════════════════════════════════════════════════════════════════════════════

def new_game(xid, xn, oid, on, chat_id):
    """
    Single shared board posted in chat_id.
    Works for both groups and DMs.
    """
    return {
        "players":   {"x": xid, "o": oid},
        "names":     {xid: xn, oid: on},
        "board":     [""] * 9,
        "turn":      "X",
        "chat_id":   chat_id,
        "msg_id":    None,
        "last_move": datetime.utcnow(),
        "finished":  False,
    }

async def send_board(game, game_id, context, intro=""):
    txt = (intro + "\n" if intro else "") + board_text(game)
    msg = await context.bot.send_message(
        chat_id      = game["chat_id"],
        text         = txt,
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = board_kb(game["board"], game_id),
    )
    game["msg_id"] = msg.message_id

async def edit_board(game, game_id, context, status=""):
    try:
        await context.bot.edit_message_text(
            chat_id      = game["chat_id"],
            message_id   = game["msg_id"],
            text         = board_text(game, status),
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = board_kb(game["board"], game_id),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"edit_board: {e}")

async def end_game(game, game_id, result, context, note=""):
    game["finished"] = True
    xid = game["players"]["x"]
    oid = game["players"]["o"]

    if result == "draw":
        db_draw(xid); db_draw(oid)
        banner = f"🤝 *It's a Draw!*  (+{PTS_DRAW} pts each)"
    else:
        wid = xid if result=="X" else oid
        lid = oid if result=="X" else xid
        db_win(wid); db_loss(lid)
        w      = fetch_user(wid)
        streak = f"  🔥 {w['win_streak']} in a row!" if w and w["win_streak"] >= 2 else ""
        banner = f"🏆 *{sym(result)} {game['names'][wid]} wins!*  (+{PTS_WIN} pts){streak}"

    if note:
        banner = note + "\n" + banner

    # Update the board message to show final state
    try:
        await context.bot.edit_message_text(
            chat_id      = game["chat_id"],
            message_id   = game["msg_id"],
            text         = board_text(game, banner),
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = board_kb(game["board"], game_id),
        )
    except Exception: pass

    # Send result + buttons in same chat
    await context.bot.send_message(
        chat_id      = game["chat_id"],
        text         = banner + "\n\n🎊 GG! Play again?",
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = after_kb(game_id),
    )

    user_to_game.pop(xid, None)
    user_to_game.pop(oid, None)
    active_games.pop(game_id, None)

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND JOB
# ═══════════════════════════════════════════════════════════════════════════════

async def bg_tick(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()

    # Timeout running games
    for gid, g in list(active_games.items()):
        if g["finished"]: continue
        if now - g["last_move"] > timedelta(seconds=TIMEOUT_SECONDS):
            ts   = g["turn"]
            tid  = g["players"][ts.lower()]
            note = f"⏰ *{g['names'][tid]}* ran out of time — forfeit!"
            await end_game(g, gid, other(ts), context, note=note)

    # Expire pending duels
    for did, d in list(pending_duels.items()):
        if now - d["created_at"] > timedelta(seconds=DUEL_EXPIRE_SEC):
            pending_duels.pop(did, None)
            try:
                await context.bot.edit_message_text(
                    chat_id    = d["chat_id"],
                    message_id = d["msg_id"],
                    text       = f"⌛ *Challenge expired!*\n_{d['cname']} vs {d['tname']}_",
                    parse_mode = ParseMode.MARKDOWN,
                )
            except Exception: pass

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    chat_type = update.effective_chat.type
    upsert_user(user.id, user.first_name)

    if chat_type == "private":
        context.bot_data[f"dm_{user.id}"] = update.effective_chat.id
        await update.message.reply_text(
            f"👋 *Hey {user.first_name}!* Welcome to *Tic Tac Toe* 🎮\n\n"
            f"Use `/duel @friend` in any group, or play random below!",
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = menu_kb(),
        )
    else:
        await update.message.reply_text(
            f"🎮 *Tic Tac Toe Bot ready!*\n\n"
            f"⚔️ `/duel @username` — challenge anyone here!\n"
            f"📊 `/stats` — your stats\n"
            f"🏆 `/leaderboard` — top 10\n\n"
            f"_No DM needed — just duel!_ 🚀",
            parse_mode = ParseMode.MARKDOWN,
        )


async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /duel @username
    Board is posted directly in the chat where command was sent.
    No DM setup required from either player.
    The ONLY requirement: opponent must have used this bot before
    (so their name is in our DB). First-time users just need to send
    any message after bot is added to group, OR use /start once.
    """
    challenger = update.effective_user
    upsert_user(challenger.id, challenger.first_name)

    msg     = update.message
    chat_id = update.effective_chat.id
    entities= msg.entities or []

    # ── Resolve target ────────────────────────────────────────────────────────
    target_id   = None
    target_name = None

    for ent in entities:
        # Case 1: inline mention (user has no username, telegram provides user object)
        if ent.type == "text_mention" and ent.user and ent.user.id != challenger.id:
            target_id   = ent.user.id
            target_name = ent.user.first_name
            upsert_user(target_id, target_name)
            break
        # Case 2: @username mention — look up in our DB
        elif ent.type == "mention":
            uname = msg.text[ent.offset:ent.offset+ent.length].lstrip("@")
            with _db() as c:
                row = c.execute(
                    "SELECT user_id, username FROM users WHERE LOWER(username)=LOWER(?)",
                    (uname,)
                ).fetchone()
            if row and row["user_id"] != challenger.id:
                target_id   = row["user_id"]
                target_name = row["username"]
                break

    if not target_id:
        await msg.reply_text(
            "⚠️ *Usage:* `/duel @username`\n\n"
            "The opponent must have chatted with this bot before.\n"
            "Ask them to send `/start` to the bot once — then duel away! 🎮",
            parse_mode = ParseMode.MARKDOWN,
        )
        return

    if target_id == challenger.id:
        await msg.reply_text("🤡 Can't duel yourself!")
        return

    if challenger.id in user_to_game:
        await msg.reply_text("⚠️ You're already in a game! Use /quit to forfeit.")
        return
    if target_id in user_to_game:
        await msg.reply_text(f"⚠️ *{target_name}* is already in a game!", parse_mode=ParseMode.MARKDOWN)
        return

    # ── Post challenge ────────────────────────────────────────────────────────
    duel_id = _did()
    cmsg    = await msg.reply_text(
        f"⚔️ *Duel Challenge!*\n\n"
        f"{E_X} *{challenger.first_name}* challenges\n"
        f"{E_O} *{target_name}* to Tic Tac Toe!\n\n"
        f"⏳ Expires in {DUEL_EXPIRE_SEC}s\n\n"
        f"*{target_name}*, do you accept?",
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = duel_kb(duel_id),
    )
    pending_duels[duel_id] = {
        "cid": challenger.id,  "cname": challenger.first_name,
        "tid": target_id,      "tname": target_name,
        "chat_id":    chat_id,
        "msg_id":     cmsg.message_id,
        "created_at": datetime.utcnow(),
    }


async def cmd_quit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    game_id = user_to_game.get(user.id)
    if not game_id:
        await update.message.reply_text("❌ You're not in a game.")
        return
    game = active_games.get(game_id)
    if not game or game["finished"]:
        user_to_game.pop(user.id, None)
        await update.message.reply_text("❌ No active game found.")
        return
    ws = "O" if user.id == game["players"]["x"] else "X"
    await end_game(game, game_id, ws, context,
                   note=f"🚪 *{game['names'][user.id]}* quit — forfeit!")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.first_name)
    row = fetch_user(user.id)
    await update.message.reply_text(stats_text(row, user.first_name), parse_mode=ParseMode.MARKDOWN)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lb_text(fetch_top10()), parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = q.from_user
    data = q.data
    await q.answer()
    upsert_user(user.id, user.first_name)

    if q.message.chat.type == "private":
        context.bot_data[f"dm_{user.id}"] = q.message.chat_id

    # ── Main menu ─────────────────────────────────────────────────────────────
    if data == "menu":
        await q.edit_message_text(
            f"🏠 *Main Menu* — hey {user.first_name}!",
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = menu_kb(),
        )

    # ── Play (DM matchmaking) ─────────────────────────────────────────────────
    elif data == "play":
        if q.message.chat.type != "private":
            await q.answer("Use /duel @username in groups!", show_alert=True)
            return
        if user.id in user_to_game:
            await q.answer("Already in a game!", show_alert=True)
            return
        if user.id in matchmaking_queue:
            await q.answer("Already in queue!", show_alert=True)
            return

        dm_chat = q.message.chat_id
        context.bot_data[f"dm_{user.id}"] = dm_chat

        if matchmaking_queue:
            opp_id, opp_chat = next(iter(matchmaking_queue.items()))
            matchmaking_queue.pop(opp_id)

            if opp_id in user_to_game:
                matchmaking_queue[user.id] = dm_chat
                await q.edit_message_text(
                    "⏳ *Searching...*",
                    parse_mode   = ParseMode.MARKDOWN,
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Leave Queue", callback_data="cq")
                    ]]),
                )
                return

            opp_row  = fetch_user(opp_id)
            opp_name = opp_row["username"] if opp_row else "Unknown"

            if random.random() < 0.5:
                xid, xn, oid, on = user.id, user.first_name, opp_id, opp_name
            else:
                xid, xn, oid, on = opp_id, opp_name, user.id, user.first_name

            game_id = _gid()
            # Board goes to the current user's DM
            game = new_game(xid, xn, oid, on, chat_id=dm_chat)
            active_games[game_id] = game
            user_to_game[user.id] = game_id
            user_to_game[opp_id]  = game_id

            await q.edit_message_text("✅ *Opponent found!*", parse_mode=ParseMode.MARKDOWN)
            await send_board(game, game_id, context,
                             intro=f"✅ *Match found!*\n{E_X} *{xn}*  vs  {E_O} *{on}*")

            # Notify opponent
            try:
                await context.bot.send_message(
                    chat_id    = opp_chat,
                    text       = f"✅ *Match found!* Playing vs *{user.first_name}* — check this chat for the board!",
                    parse_mode = ParseMode.MARKDOWN,
                )
            except Exception: pass
        else:
            matchmaking_queue[user.id] = dm_chat
            await q.edit_message_text(
                "⏳ *Searching for opponent...*\n\nYou'll be matched automatically!",
                parse_mode   = ParseMode.MARKDOWN,
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Leave Queue", callback_data="cq")
                ]]),
            )

    elif data == "cq":
        matchmaking_queue.pop(user.id, None)
        await q.edit_message_text(
            "✅ Left the queue.",
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]]),
        )

    elif data == "leaderboard":
        await q.edit_message_text(
            lb_text(fetch_top10()),
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh",   callback_data="leaderboard"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu"),
            ]]),
        )

    elif data == "stats":
        row = fetch_user(user.id)
        if not row:
            await q.answer("Play a game first!", show_alert=True)
            return
        await q.edit_message_text(
            stats_text(row, user.first_name),
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]]),
        )

    elif data == "help":
        await q.edit_message_text(
            HELP_MSG,
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]]),
        )

    # ── Duel accept ───────────────────────────────────────────────────────────
    elif data.startswith("da:"):
        duel_id = int(data.split(":")[1])
        duel    = pending_duels.get(duel_id)

        if not duel:
            await q.answer("⌛ Challenge expired!", show_alert=True)
            return
        if user.id != duel["tid"]:
            await q.answer("❌ This challenge isn't for you!", show_alert=True)
            return

        pending_duels.pop(duel_id, None)
        cid, cname = duel["cid"], duel["cname"]
        tname      = duel["tname"]

        if cid in user_to_game:
            await q.edit_message_text(f"⚠️ *{cname}* already in another game.", parse_mode=ParseMode.MARKDOWN)
            return
        if user.id in user_to_game:
            await q.edit_message_text("⚠️ You're already in a game! Use /quit first.", parse_mode=ParseMode.MARKDOWN)
            return

        if random.random() < 0.5:
            xid, xn, oid, on = cid, cname, user.id, tname
        else:
            xid, xn, oid, on = user.id, tname, cid, cname

        game_id = _gid()
        # ★ Board goes to the SAME chat as the duel challenge — no DM needed!
        game = new_game(xid, xn, oid, on, chat_id=duel["chat_id"])
        active_games[game_id] = game
        user_to_game[cid]     = game_id
        user_to_game[user.id] = game_id

        intro = f"✅ *{tname}* accepted!\n{E_X} *{xn}*  vs  {E_O} *{on}*"
        try:
            await q.edit_message_text(intro, parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

        await send_board(game, game_id, context, intro=intro)

    # ── Duel decline ──────────────────────────────────────────────────────────
    elif data.startswith("dd:"):
        duel_id = int(data.split(":")[1])
        duel    = pending_duels.get(duel_id)
        if not duel:
            await q.answer("Already expired.", show_alert=True)
            return
        if user.id != duel["tid"]:
            await q.answer("Not for you!", show_alert=True)
            return
        pending_duels.pop(duel_id, None)
        await q.edit_message_text(
            f"❌ *{duel['tname']}* declined.\nBetter luck next time, *{duel['cname']}*! 😔",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Move ──────────────────────────────────────────────────────────────────
    elif data.startswith("mv:"):
        _, gid_s, idx_s = data.split(":")
        game_id  = int(gid_s)
        cell_idx = int(idx_s)

        game = active_games.get(game_id)
        if not game or game["finished"]:
            await q.answer("This game has ended.", show_alert=True)
            return

        cur_sym = game["turn"]
        cur_id  = game["players"][cur_sym.lower()]

        if user.id != cur_id:
            opp_name = game["names"][cur_id]
            await q.answer(f"Not your turn! Wait for {opp_name}.", show_alert=True)
            return
        if game["board"][cell_idx]:
            await q.answer("Cell already taken!", show_alert=True)
            return

        game["board"][cell_idx] = cur_sym
        game["last_move"]       = datetime.utcnow()

        result = check_result(game["board"])
        if result:
            await end_game(game, game_id, result, context)
        else:
            game["turn"] = other(cur_sym)
            await edit_board(game, game_id, context)

    # ── Rematch ───────────────────────────────────────────────────────────────
    elif data.startswith("rematch:"):
        old_gid = int(data.split(":")[1])
        rkey    = f"rm_{old_gid}"

        if rkey not in context.bot_data:
            context.bot_data[rkey] = {
                "uid": user.id, "name": user.first_name,
                "chat_id": q.message.chat_id,
            }
            await q.edit_message_text(
                "⏳ *Rematch requested!*\n\nWaiting for opponent to also press 🔄...",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            other_data = context.bot_data.pop(rkey)
            oid   = other_data["uid"]
            oname = other_data["name"]

            if user.id == oid:
                context.bot_data[rkey] = other_data
                await q.answer("Waiting for your opponent!", show_alert=True)
                return
            if user.id in user_to_game or oid in user_to_game:
                await q.edit_message_text(
                    "⚠️ One of you is already in another game.",
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 Menu", callback_data="menu")
                    ]]),
                )
                return

            if random.random() < 0.5:
                xid, xn, yid, yn = user.id, user.first_name, oid, oname
            else:
                xid, xn, yid, yn = oid, oname, user.id, user.first_name

            game_id = _gid()
            game    = new_game(xid, xn, yid, yn, chat_id=q.message.chat_id)
            active_games[game_id]  = game
            user_to_game[user.id]  = game_id
            user_to_game[oid]      = game_id

            await q.edit_message_text("🔄 *Rematch starting!*", parse_mode=ParseMode.MARKDOWN)
            await send_board(game, game_id, context,
                             intro=f"🔄 *Rematch!*\n{E_X} *{xn}*  vs  {E_O} *{yn}*")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("❌ Set TELEGRAM_BOT_TOKEN environment variable!")

    init_db()
    logger.info("✅ DB ready")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("duel",        cmd_duel))
    app.add_handler(CommandHandler("quit",        cmd_quit))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.job_queue.run_repeating(bg_tick, interval=CHECK_INTERVAL, first=CHECK_INTERVAL)

    logger.info("🚀 Bot running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
