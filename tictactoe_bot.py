#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║        🎮  TIC TAC TOE  TELEGRAM  BOT  🎮           ║
║  ─────────────────────────────────────────────────  ║
║  • PvP matchmaking queue (private chat)             ║
║  • /duel @user — challenge anyone in a group        ║
║  • Shared live board in group chats                 ║
║  • Win streak, leaderboard, stats, rematch          ║
║  • 60-second inactivity timeout (forfeit)           ║
╚══════════════════════════════════════════════════════╝

Install:
    pip install "python-telegram-bot[job-queue]==20.7"

Run:
    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    python tictactoe_bot.py
"""

# ─── Standard library ────────────────────────────────────────────────────────
import os
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

# ─── python-telegram-bot 20.x ────────────────────────────────────────────────
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH  = "tictactoe.db"

TIMEOUT_SECONDS   = 60   # inactivity → forfeit
DUEL_EXPIRE_SEC   = 60   # challenge expires after 60s
CHECK_INTERVAL    = 10   # job runs every 10 seconds

# Points awarded
PTS_WIN  = 10
PTS_DRAW = 3
PTS_LOSS = 0

# Board cell emojis
E_EMPTY = "⬜"
E_X     = "❌"
E_O     = "⭕"

# Logging
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY STATE  (lives for the life of the process)
# ═══════════════════════════════════════════════════════════════════════════════

# game_id → game dict  (see _new_game() for schema)
active_games: dict[int, dict] = {}

# user_id → game_id  (quick look-up; prevents double-joining)
user_to_game: dict[int, int] = {}

# FIFO queue of user_ids waiting for a random opponent (private chat only)
matchmaking_queue: list[int] = {}  # {user_id: chat_id}
matchmaking_queue: dict[int, int] = {}

# challenge_id → pending duel dict
pending_duels: dict[int, dict] = {}

_game_counter   = 0
_duel_counter   = 0

def _next_game_id() -> int:
    global _game_counter
    _game_counter += 1
    return _game_counter

def _next_duel_id() -> int:
    global _duel_counter
    _duel_counter += 1
    return _duel_counter

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Create the users table if it doesn't exist."""
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

def upsert_user(user_id: int, username: str) -> None:
    """Insert user or update their display name."""
    with _db() as c:
        c.execute("""
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
        """, (user_id, username or "Unknown"))
        c.commit()

def fetch_user(user_id: int) -> Optional[sqlite3.Row]:
    with _db() as c:
        return c.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

def db_record_win(uid: int) -> None:
    with _db() as c:
        c.execute("""
            UPDATE users SET
                wins        = wins + 1,
                points      = points + ?,
                win_streak  = win_streak + 1,
                best_streak = MAX(best_streak, win_streak + 1)
            WHERE user_id = ?
        """, (PTS_WIN, uid))
        c.commit()

def db_record_loss(uid: int) -> None:
    with _db() as c:
        c.execute("""
            UPDATE users SET
                losses     = losses + 1,
                win_streak = 0
            WHERE user_id = ?
        """, (uid,))
        c.commit()

def db_record_draw(uid: int) -> None:
    with _db() as c:
        c.execute("""
            UPDATE users SET
                draws      = draws + 1,
                points     = points + ?,
                win_streak = 0
            WHERE user_id = ?
        """, (PTS_DRAW, uid))
        c.commit()

def fetch_leaderboard() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("""
            SELECT username, wins, losses, draws, points, best_streak
            FROM   users
            ORDER  BY points DESC, wins DESC
            LIMIT  10
        """).fetchall()

# ═══════════════════════════════════════════════════════════════════════════════
# BOARD LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

WIN_LINES = [
    (0,1,2), (3,4,5), (6,7,8),   # rows
    (0,3,6), (1,4,7), (2,5,8),   # cols
    (0,4,8), (2,4,6),             # diagonals
]

def empty_board() -> list[str]:
    return [""] * 9

def check_result(board: list[str]) -> Optional[str]:
    """Return 'X', 'O', 'draw', or None (game ongoing)."""
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None

def get_winning_cells(board: list[str]) -> Optional[tuple]:
    """Return the winning triple of indices, or None."""
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return (a, b, c)
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# GAME FACTORY  &  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _new_game(
    x_id: int, x_name: str,
    o_id: int, o_name: str,
    is_group: bool = False,
    group_chat_id: int = 0,
) -> dict:
    """
    Schema:
        players   : {"x": uid, "o": uid}
        names     : {uid: display_name}
        board     : list[9]  ("" | "X" | "O")
        turn      : "X" | "O"
        is_group  : bool   — shared board in a group, or separate DM boards
        group_chat_id : int
        chat_ids  : {uid: chat_id}           — for DM games
        msg_ids   : {uid: message_id}        — board message to edit per player
        group_msg_id : int                   — group shared board message
        last_move : datetime
        finished  : bool
    """
    return {
        "players":      {"x": x_id, "o": o_id},
        "names":        {x_id: x_name, o_id: o_name},
        "board":        empty_board(),
        "turn":         "X",
        "is_group":     is_group,
        "group_chat_id": group_chat_id,
        "chat_ids":     {},
        "msg_ids":      {},
        "group_msg_id": None,
        "last_move":    datetime.utcnow(),
        "finished":     False,
    }

def _sym(s: str) -> str:
    return E_X if s == "X" else E_O

def _other(s: str) -> str:
    return "O" if s == "X" else "X"

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def board_keyboard(board: list[str], game_id: int, winning_cells=None) -> InlineKeyboardMarkup:
    """
    Build the 3×3 clickable grid.
    Winning cells are highlighted (they'll appear naturally because their symbol
    is still shown; we just mark them in callback as "win_cell" so they can't
    be re-clicked, but visually they look the same — no extra library needed).
    """
    sym_map = {"X": E_X, "O": E_O, "": E_EMPTY}
    rows = []
    for r in range(3):
        row_btns = []
        for col in range(3):
            idx = r * 3 + col
            label = sym_map[board[idx]]
            cb = f"move:{game_id}:{idx}"
            row_btns.append(InlineKeyboardButton(label, callback_data=cb))
        rows.append(row_btns)
    return InlineKeyboardMarkup(rows)

def after_game_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Play Again",  callback_data=f"rematch:{game_id}"),
        InlineKeyboardButton("🏠 Main Menu",   callback_data="menu"),
    ]])

def duel_keyboard(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"duel_accept:{duel_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"duel_decline:{duel_id}"),
    ]])

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮  Play Game",    callback_data="play")],
        [InlineKeyboardButton("🏆  Leaderboard",  callback_data="leaderboard"),
         InlineKeyboardButton("📊  My Stats",     callback_data="stats")],
        [InlineKeyboardButton("ℹ️  Help",         callback_data="help")],
    ])

def cancel_queue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Leave Queue", callback_data="cancel_queue")
    ]])

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_board_header(game: dict, status: str = "") -> str:
    """Top text above the board keyboard."""
    xid = game["players"]["x"]
    oid = game["players"]["o"]
    xn  = game["names"][xid]
    on  = game["names"][oid]

    turn_id  = game["players"][game["turn"].lower()]
    turn_sym = _sym(game["turn"])
    turn_name= game["names"][turn_id]

    header = (
        f"🎮 *Tic Tac Toe*\n"
        f"{E_X} *{xn}*  vs  {E_O} *{on}*\n"
        f"{'─'*28}\n"
    )
    body = status if status else f"🕹️  {turn_sym} *{turn_name}*'s turn"
    return header + body

def fmt_stats(row: sqlite3.Row, name: str) -> str:
    total   = row["wins"] + row["losses"] + row["draws"]
    winrate = f"{row['wins']/total*100:.1f}%" if total else "—"
    return (
        f"📊 *Stats — {name}*\n"
        f"{'─'*26}\n"
        f"🏆  Wins         *{row['wins']}*\n"
        f"❌  Losses       *{row['losses']}*\n"
        f"🤝  Draws        *{row['draws']}*\n"
        f"🎮  Games played *{total}*\n"
        f"📈  Win rate     *{winrate}*\n"
        f"{'─'*26}\n"
        f"⭐  Points       *{row['points']}*\n"
        f"🔥  Streak       *{row['win_streak']}*\n"
        f"🏅  Best streak  *{row['best_streak']}*"
    )

def fmt_leaderboard(rows: list) -> str:
    if not rows:
        return "_No players yet. Be the first!_ 🌟"
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = ["🏆 *Global Leaderboard — Top 10*", "─"*32]
    for i, r in enumerate(rows):
        lines.append(
            f"{medals[i]} *{r['username']}*\n"
            f"    ⭐ {r['points']} pts  │  "
            f"✅ {r['wins']}W  ❌ {r['losses']}L  🤝 {r['draws']}D  "
            f"│  🔥 best {r['best_streak']}"
        )
    return "\n".join(lines)

HELP_TEXT = (
    "ℹ️ *How to Play*\n"
    "─"*28 + "\n\n"
    "*Private Chat (random match):*\n"
    "  1. Send /start to the bot in DM\n"
    "  2. Tap 🎮 Play Game — enter queue\n"
    "  3. Bot matches you with another player\n"
    "  4. Tap a square on the board to move\n"
    "  5. First to get 3 in a row wins!\n\n"
    "*Group Chat (duel):*\n"
    "  `/duel @username` — challenge anyone\n"
    "  The opponent taps ✅ Accept to start\n"
    "  Both players use the shared board\n\n"
    "⏰ *Timeout:* No move in 60s = forfeit\n"
    "🚪 */quit* — forfeit your current game\n\n"
    "*Points*\n"
    f"  🏆 Win  → +{PTS_WIN} pts\n"
    f"  🤝 Draw → +{PTS_DRAW} pts\n"
    f"  ❌ Loss →  0 pts"
)

# ═══════════════════════════════════════════════════════════════════════════════
# CORE: START & UPDATE BOARDS
# ═══════════════════════════════════════════════════════════════════════════════

async def launch_game(
    game: dict,
    game_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    announce_text: str = "",
) -> None:
    """
    Send the initial board to all participants.
    - Group game  → one shared message in the group
    - Private game → one message per player in their DM
    """
    kb   = board_keyboard(game["board"], game_id)
    text = fmt_board_header(game)

    if game["is_group"]:
        intro = announce_text or "🎮 *Game on!* Let's go!\n\n"
        msg = await context.bot.send_message(
            chat_id   = game["group_chat_id"],
            text      = intro + text,
            parse_mode= ParseMode.MARKDOWN,
            reply_markup = kb,
        )
        game["group_msg_id"] = msg.message_id
    else:
        # Send individual boards to each player's DM
        for sym in ("x", "o"):
            uid  = game["players"][sym]
            cid  = game["chat_ids"].get(uid)
            if not cid:
                continue
            intro = (
                f"✅ *Opponent found!*\n"
                f"You are {_sym(sym.upper())} *{sym.upper()}*\n"
                f"vs {E_X if sym=='o' else E_O} *{game['names'][game['players']['o' if sym=='x' else 'x']]}*\n\n"
            )
            msg = await context.bot.send_message(
                chat_id    = cid,
                text       = intro + text,
                parse_mode = ParseMode.MARKDOWN,
                reply_markup = kb,
            )
            game["msg_ids"][uid] = msg.message_id


async def refresh_board(
    game: dict,
    game_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    status: str = "",
) -> None:
    """Edit existing board message(s) with the latest state."""
    kb   = board_keyboard(game["board"], game_id)
    text = fmt_board_header(game, status)

    if game["is_group"]:
        cid = game["group_chat_id"]
        mid = game["group_msg_id"]
        if cid and mid:
            await _safe_edit(context, cid, mid, text, kb)
    else:
        for uid in [game["players"]["x"], game["players"]["o"]]:
            cid = game["chat_ids"].get(uid)
            mid = game["msg_ids"].get(uid)
            if cid and mid:
                await _safe_edit(context, cid, mid, text, kb)


async def _safe_edit(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int, message_id: int,
    text: str, reply_markup,
) -> None:
    try:
        await context.bot.edit_message_text(
            chat_id      = chat_id,
            message_id   = message_id,
            text         = text,
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = reply_markup,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Edit failed chat={chat_id} msg={message_id}: {e}")
    except Exception as e:
        logger.warning(f"Edit error: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# CORE: FINISH GAME
# ═══════════════════════════════════════════════════════════════════════════════

async def finish_game(
    game: dict,
    game_id: int,
    result: str,   # "X" | "O" | "draw"
    context: ContextTypes.DEFAULT_TYPE,
    extra_note: str = "",
) -> None:
    """
    Record result in DB, update board one final time,
    then send a result banner with Play Again / Main Menu buttons.
    """
    game["finished"] = True
    x_id = game["players"]["x"]
    o_id = game["players"]["o"]

    if result == "draw":
        db_record_draw(x_id)
        db_record_draw(o_id)
        banner = f"🤝 *It's a Draw!*  (+{PTS_DRAW} pts each)"
    else:
        winner_id = x_id if result == "X" else o_id
        loser_id  = o_id if result == "X" else x_id
        db_record_win(winner_id)
        db_record_loss(loser_id)
        w_row = fetch_user(winner_id)
        streak = w_row["win_streak"] if w_row else 1
        streak_txt = f"  🔥 {streak} in a row!" if streak >= 2 else ""
        banner = (
            f"🏆 *{_sym(result)} {game['names'][winner_id]} wins!*"
            f"  (+{PTS_WIN} pts){streak_txt}"
        )

    if extra_note:
        banner = extra_note + "\n" + banner

    # Show final board state
    final_kb = board_keyboard(game["board"], game_id)
    final_text = fmt_board_header(game, banner)
    after_kb   = after_game_keyboard(game_id)

    if game["is_group"]:
        cid = game["group_chat_id"]
        mid = game["group_msg_id"]
        if cid and mid:
            await _safe_edit(context, cid, mid, final_text, final_kb)
            await context.bot.send_message(
                chat_id    = cid,
                text       = banner + "\n\nGG! 🎊",
                parse_mode = ParseMode.MARKDOWN,
                reply_markup = after_kb,
            )
    else:
        for uid in [x_id, o_id]:
            cid = game["chat_ids"].get(uid)
            mid = game["msg_ids"].get(uid)
            if cid and mid:
                await _safe_edit(context, cid, mid, final_text, final_kb)
                await context.bot.send_message(
                    chat_id    = cid,
                    text       = banner + "\n\nGG! 🎊",
                    parse_mode = ParseMode.MARKDOWN,
                    reply_markup = after_kb,
                )

    # Cleanup
    for uid in [x_id, o_id]:
        user_to_game.pop(uid, None)
    active_games.pop(game_id, None)

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND JOB: TIMEOUT & DUEL EXPIRY
# ═══════════════════════════════════════════════════════════════════════════════

async def bg_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every CHECK_INTERVAL seconds to handle timeouts and expired duels."""
    now = datetime.utcnow()

    # ── 1. Game timeouts ──────────────────────────────────────────────────────
    timed_out = [
        (gid, g) for gid, g in list(active_games.items())
        if not g["finished"]
        and now - g["last_move"] > timedelta(seconds=TIMEOUT_SECONDS)
    ]
    for gid, game in timed_out:
        # The player whose turn it is gets the timeout
        timed_sym  = game["turn"]
        winner_sym = _other(timed_sym)
        timed_id   = game["players"][timed_sym.lower()]
        winner_id  = game["players"][winner_sym.lower()]
        timed_name = game["names"][timed_id]

        note = f"⏰ *{timed_name}* ran out of time — forfeit!"
        await finish_game(game, gid, winner_sym, context, extra_note=note)

    # ── 2. Duel challenge expiry ──────────────────────────────────────────────
    expired = [
        did for did, d in list(pending_duels.items())
        if now - d["created_at"] > timedelta(seconds=DUEL_EXPIRE_SEC)
    ]
    for did in expired:
        duel = pending_duels.pop(did, None)
        if not duel:
            continue
        try:
            await context.bot.edit_message_text(
                chat_id    = duel["chat_id"],
                message_id = duel["msg_id"],
                text       = (
                    f"⌛ *Challenge expired!*\n"
                    f"*{duel['challenger_name']}* vs *{duel['target_name']}*\n"
                    f"No response within {DUEL_EXPIRE_SEC}s."
                ),
                parse_mode = ParseMode.MARKDOWN,
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show main menu. Works in DM only for the menu."""
    user = update.effective_user
    upsert_user(user.id, user.first_name)

    # Store the DM chat id so matchmaker can find the player later
    context.bot_data[f"dm_{user.id}"] = update.effective_chat.id

    welcome = (
        f"👋 *Hey {user.first_name}!*\n\n"
        f"Welcome to *Tic Tac Toe Bot* 🎮\n"
        f"Challenge strangers or duel your friends!\n\n"
        f"Choose an option below:"
    )
    await update.message.reply_text(
        welcome,
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = main_menu_keyboard(),
    )


async def cmd_quit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/quit — forfeit the active game."""
    user    = update.effective_user
    game_id = user_to_game.get(user.id)

    if not game_id:
        await update.message.reply_text("❌ You're not in a game right now.")
        return

    game = active_games.get(game_id)
    if not game or game["finished"]:
        user_to_game.pop(user.id, None)
        await update.message.reply_text("❌ No active game found.")
        return

    x_id = game["players"]["x"]
    o_id = game["players"]["o"]
    winner_id  = o_id if user.id == x_id else x_id
    winner_sym = "O"  if user.id == x_id else "X"
    note = f"🚪 *{game['names'][user.id]}* quit — forfeit!"

    await finish_game(game, game_id, winner_sym, context, extra_note=note)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — show caller's stats."""
    user = update.effective_user
    upsert_user(user.id, user.first_name)
    row  = fetch_user(user.id)
    await update.message.reply_text(
        fmt_stats(row, user.first_name),
        parse_mode = ParseMode.MARKDOWN,
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/leaderboard — show top 10."""
    rows = fetch_leaderboard()
    await update.message.reply_text(
        fmt_leaderboard(rows),
        parse_mode = ParseMode.MARKDOWN,
    )


async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /duel @username  — challenge a specific player.
    Works in both groups and private chats.
    """
    challenger = update.effective_user
    upsert_user(challenger.id, challenger.first_name)

    # ── Parse target from mention ─────────────────────────────────────────────
    # The target can be a text_mention entity (no username) or a @username mention
    msg      = update.message
    entities = msg.entities or []
    target_user = None

    for ent in entities:
        if ent.type == "text_mention" and ent.user:
            if ent.user.id != challenger.id:
                target_user = ent.user
                break
        elif ent.type == "mention":
            # Extract the @username from text
            mention = msg.text[ent.offset : ent.offset + ent.length]  # e.g. "@Alice"
            username = mention.lstrip("@")
            # We can't resolve @username → user_id without them having messaged the bot,
            # so we look it up in our DB
            with _db() as c:
                row = c.execute(
                    "SELECT user_id, username FROM users WHERE LOWER(username)=LOWER(?)",
                    (username,)
                ).fetchone()
            if row and row["user_id"] != challenger.id:
                # Build a minimal User-like object
                class _FakeUser:
                    id         = row["user_id"]
                    first_name = row["username"]
                target_user = _FakeUser()
                break

    if not target_user:
        await msg.reply_text(
            "⚠️ Usage: `/duel @username`\n\n"
            "_The target must have used this bot at least once._",
            parse_mode = ParseMode.MARKDOWN,
        )
        return

    if target_user.id == challenger.id:
        await msg.reply_text("🤡 You can't duel yourself!")
        return

    # ── Prevent double-games ──────────────────────────────────────────────────
    if challenger.id in user_to_game:
        await msg.reply_text("⚠️ You're already in a game! Use /quit to forfeit first.")
        return
    if target_user.id in user_to_game:
        tname = getattr(target_user, "first_name", "That player")
        await msg.reply_text(f"⚠️ *{tname}* is already in a game!", parse_mode=ParseMode.MARKDOWN)
        return

    # ── Post the challenge ────────────────────────────────────────────────────
    duel_id = _next_duel_id()
    tname   = getattr(target_user, "first_name", "Unknown")
    upsert_user(target_user.id, tname)

    challenge_text = (
        f"⚔️ *Duel Challenge!*\n\n"
        f"{E_X} *{challenger.first_name}* has challenged\n"
        f"{E_O} *{tname}* to Tic Tac Toe!\n\n"
        f"⏳ Challenge expires in {DUEL_EXPIRE_SEC}s\n\n"
        f"*{tname}*, do you accept?"
    )
    challenge_msg = await msg.reply_text(
        challenge_text,
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = duel_keyboard(duel_id),
    )

    pending_duels[duel_id] = {
        "challenger_id":   challenger.id,
        "challenger_name": challenger.first_name,
        "target_id":       target_user.id,
        "target_name":     tname,
        "chat_id":         update.effective_chat.id,
        "msg_id":          challenge_msg.message_id,
        "is_group":        update.effective_chat.type in ("group", "supergroup"),
        "created_at":      datetime.utcnow(),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER  (all button presses)
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    user = q.from_user
    data = q.data

    await q.answer()   # acknowledge immediately so spinner disappears
    upsert_user(user.id, user.first_name)

    # Store DM chat id whenever we see the user
    if q.message.chat.type == "private":
        context.bot_data[f"dm_{user.id}"] = q.message.chat_id

    # ── Route by prefix ───────────────────────────────────────────────────────

    if data == "menu":
        await _cb_menu(q, user)

    elif data == "play":
        await _cb_play(q, user, context)

    elif data == "cancel_queue":
        await _cb_cancel_queue(q, user, context)

    elif data == "leaderboard":
        await _cb_leaderboard(q)

    elif data == "stats":
        await _cb_stats(q, user)

    elif data == "help":
        await q.edit_message_text(
            HELP_TEXT,
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
            ]]),
        )

    elif data.startswith("move:"):
        await _cb_move(q, user, data, context)

    elif data.startswith("rematch:"):
        await _cb_rematch(q, user, data, context)

    elif data.startswith("duel_accept:"):
        await _cb_duel_accept(q, user, data, context)

    elif data.startswith("duel_decline:"):
        await _cb_duel_decline(q, user, data)


# ─── individual callback implementations ─────────────────────────────────────

async def _cb_menu(q, user) -> None:
    await q.edit_message_text(
        f"🏠 *Main Menu*\n\nHello {user.first_name}! What would you like to do?",
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = main_menu_keyboard(),
    )

async def _cb_cancel_queue(q, user, context) -> None:
    if user.id in matchmaking_queue:
        matchmaking_queue.pop(user.id)
        context.bot_data.pop(f"dm_{user.id}_in_queue", None)
    await q.edit_message_text(
        "✅ You've left the matchmaking queue.",
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
        ]]),
    )

async def _cb_leaderboard(q) -> None:
    rows = fetch_leaderboard()
    await q.edit_message_text(
        fmt_leaderboard(rows),
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",   callback_data="leaderboard"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu"),
        ]]),
    )

async def _cb_stats(q, user) -> None:
    row = fetch_user(user.id)
    if not row:
        await q.edit_message_text("❌ Play a game first to see your stats!")
        return
    await q.edit_message_text(
        fmt_stats(row, user.first_name),
        parse_mode   = ParseMode.MARKDOWN,
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
        ]]),
    )

async def _cb_play(q, user, context) -> None:
    """Enter matchmaking queue (private chat only)."""
    if q.message.chat.type != "private":
        await q.answer("🎮 Use /duel @username to challenge someone here!", show_alert=True)
        return

    if user.id in user_to_game:
        await q.answer("⚠️ You're already in a game!", show_alert=True)
        return

    if user.id in matchmaking_queue:
        await q.answer("⏳ You're already in the queue!", show_alert=True)
        return

    dm_chat = q.message.chat_id
    context.bot_data[f"dm_{user.id}"] = dm_chat

    # Check if there's someone waiting already
    if matchmaking_queue:
        # Pop the first person in queue
        opp_id, opp_chat = next(iter(matchmaking_queue.items()))
        matchmaking_queue.pop(opp_id)

        if opp_id in user_to_game:
            # They joined a game while queuing — requeue self
            matchmaking_queue[user.id] = dm_chat
            await q.edit_message_text(
                "⏳ *Searching for opponent...*\n\nYou'll be notified when matched!",
                parse_mode   = ParseMode.MARKDOWN,
                reply_markup = cancel_queue_keyboard(),
            )
            return

        opp_row  = fetch_user(opp_id)
        opp_name = opp_row["username"] if opp_row else "Unknown"

        # Assign X/O randomly
        if random.random() < 0.5:
            x_id, x_name, o_id, o_name = user.id, user.first_name, opp_id, opp_name
        else:
            x_id, x_name, o_id, o_name = opp_id, opp_name, user.id, user.first_name

        game_id = _next_game_id()
        game    = _new_game(x_id, x_name, o_id, o_name, is_group=False)
        game["chat_ids"][user.id]  = dm_chat
        game["chat_ids"][opp_id]   = opp_chat

        active_games[game_id] = game
        user_to_game[user.id] = game_id
        user_to_game[opp_id]  = game_id

        await q.edit_message_text("✅ *Opponent found! Game starting...*", parse_mode=ParseMode.MARKDOWN)
        await launch_game(game, game_id, context)
    else:
        # Join queue
        matchmaking_queue[user.id] = dm_chat
        await q.edit_message_text(
            "⏳ *Searching for opponent...*\n\n"
            "You'll be notified as soon as someone joins!\n\n"
            "🔍 Position in queue: *1*",
            parse_mode   = ParseMode.MARKDOWN,
            reply_markup = cancel_queue_keyboard(),
        )


async def _cb_move(q, user, data: str, context) -> None:
    """Handle a board cell tap."""
    _, gid_s, idx_s = data.split(":")
    game_id  = int(gid_s)
    cell_idx = int(idx_s)

    game = active_games.get(game_id)
    if not game or game["finished"]:
        await q.answer("⚠️ This game has already ended.", show_alert=True)
        return

    # Verify it's this player's turn
    current_sym = game["turn"]
    current_id  = game["players"][current_sym.lower()]

    if user.id != current_id:
        await q.answer(f"⏳ Wait — it's not your turn!", show_alert=True)
        return

    if game["board"][cell_idx] != "":
        await q.answer("❌ That cell is already taken!", show_alert=True)
        return

    # Apply move
    game["board"][cell_idx] = current_sym
    game["last_move"]        = datetime.utcnow()

    result = check_result(game["board"])
    if result:
        await finish_game(game, game_id, result, context)
    else:
        game["turn"] = _other(current_sym)
        await refresh_board(game, game_id, context)


async def _cb_rematch(q, user, data: str, context) -> None:
    """Both players must press 'Play Again' to trigger a rematch."""
    _, gid_s = data.split(":")
    old_gid  = int(gid_s)
    rkey     = f"rematch_{old_gid}"

    if rkey not in context.bot_data:
        # First player to press — store their intent
        context.bot_data[rkey] = {
            "uid":     user.id,
            "name":    user.first_name,
            "chat_id": q.message.chat_id,
        }
        await q.edit_message_text(
            "⏳ *Rematch requested!*\n\nWaiting for your opponent to also press 🔄 Play Again...",
            parse_mode = ParseMode.MARKDOWN,
        )
    else:
        # Second player — start the rematch
        other    = context.bot_data.pop(rkey)
        other_id   = other["uid"]
        other_name = other["name"]
        other_chat = other["chat_id"]

        if user.id == other_id:
            # Same player pressed twice — ignore
            await q.answer("Waiting for your opponent!", show_alert=True)
            context.bot_data[rkey] = other  # put it back
            return

        if user.id in user_to_game or other_id in user_to_game:
            await q.edit_message_text(
                "⚠️ One of you has already joined another game.",
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu")
                ]]),
            )
            return

        if random.random() < 0.5:
            x_id, x_name, o_id, o_name = user.id, user.first_name, other_id, other_name
        else:
            x_id, x_name, o_id, o_name = other_id, other_name, user.id, user.first_name

        game_id = _next_game_id()

        # Determine if it's a group or private rematch
        is_group = q.message.chat.type in ("group", "supergroup")
        game     = _new_game(x_id, x_name, o_id, o_name, is_group=is_group,
                             group_chat_id=q.message.chat_id if is_group else 0)

        if not is_group:
            game["chat_ids"][user.id]  = q.message.chat_id
            game["chat_ids"][other_id] = other_chat

        active_games[game_id] = game
        user_to_game[user.id]  = game_id
        user_to_game[other_id] = game_id

        await q.edit_message_text("✅ *Rematch starting!* 🔄", parse_mode=ParseMode.MARKDOWN)
        await launch_game(game, game_id, context, announce_text="🔄 *Rematch!* Let's go again!\n\n")


async def _cb_duel_accept(q, user, data: str, context) -> None:
    """Target player accepts the duel challenge."""
    _, did_s = data.split(":")
    duel_id  = int(did_s)
    duel     = pending_duels.get(duel_id)

    if not duel:
        await q.answer("⌛ This challenge has already expired.", show_alert=True)
        return

    if user.id != duel["target_id"]:
        await q.answer("❌ This challenge isn't for you!", show_alert=True)
        return

    pending_duels.pop(duel_id, None)

    c_id   = duel["challenger_id"]
    c_name = duel["challenger_name"]
    t_name = duel["target_name"]

    # Check both still free
    if c_id in user_to_game:
        await q.edit_message_text(
            f"⚠️ *{c_name}* has already started another game.",
            parse_mode = ParseMode.MARKDOWN,
        )
        return
    if user.id in user_to_game:
        await q.edit_message_text(
            "⚠️ You're already in a game! Use /quit first.",
            parse_mode = ParseMode.MARKDOWN,
        )
        return

    # Randomly assign X/O
    if random.random() < 0.5:
        x_id, x_name, o_id, o_name = c_id, c_name, user.id, t_name
    else:
        x_id, x_name, o_id, o_name = user.id, t_name, c_id, c_name

    is_group = duel["is_group"]
    game_id  = _next_game_id()
    game     = _new_game(x_id, x_name, o_id, o_name,
                         is_group=is_group,
                         group_chat_id=duel["chat_id"] if is_group else 0)

    if not is_group:
        # Private duel — we need both DM chat ids
        c_dm = context.bot_data.get(f"dm_{c_id}")
        t_dm = context.bot_data.get(f"dm_{user.id}", q.message.chat_id)
        if not c_dm:
            await q.edit_message_text(
                f"⚠️ *{c_name}* hasn't started the bot in DM yet.\n"
                f"Ask them to send /start to me first!",
                parse_mode = ParseMode.MARKDOWN,
            )
            return
        game["chat_ids"][c_id]   = c_dm
        game["chat_ids"][user.id] = t_dm

    active_games[game_id]  = game
    user_to_game[c_id]     = game_id
    user_to_game[user.id]  = game_id

    intro = (
        f"✅ *{t_name}* accepted the challenge!\n\n"
        f"{E_X} *{x_name}*  vs  {E_O} *{o_name}*\n\n"
    )
    await q.edit_message_text(intro + fmt_board_header(game),
                               parse_mode=ParseMode.MARKDOWN)

    await launch_game(game, game_id, context, announce_text=intro)


async def _cb_duel_decline(q, user, data: str) -> None:
    """Target player declines the duel challenge."""
    _, did_s = data.split(":")
    duel_id  = int(did_s)
    duel     = pending_duels.get(duel_id)

    if not duel:
        await q.answer("⌛ Challenge already expired.", show_alert=True)
        return

    if user.id != duel["target_id"]:
        await q.answer("❌ This challenge isn't for you!", show_alert=True)
        return

    pending_duels.pop(duel_id, None)
    await q.edit_message_text(
        f"❌ *{duel['target_name']}* declined the challenge.\n\n"
        f"Better luck next time, *{duel['challenger_name']}*! 😔",
        parse_mode = ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "\n\n❌  TELEGRAM_BOT_TOKEN is not set!\n"
            "    export TELEGRAM_BOT_TOKEN='123456:ABC-DEF...'\n"
        )

    init_db()
    logger.info("✅ Database ready")

    app = Application.builder().token(TOKEN).build()

    # ── Commands ───────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("quit",        cmd_quit))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("duel",        cmd_duel))

    # ── Inline button callbacks ────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(handle_callback))

    # ── Background tick (timeout + duel expiry) ────────────────────────────────
    app.job_queue.run_repeating(bg_tick, interval=CHECK_INTERVAL, first=CHECK_INTERVAL)

    logger.info("🚀 Bot is running — press Ctrl+C to stop")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
