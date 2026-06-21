from zoneinfo import ZoneInfo
from datetime import datetime
import os
import json
import time
import re
import asyncio
import urllib.request
import urllib.parse
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
RULES_FILE = BASE_DIR / "rules.json"
DB_FILE = BASE_DIR / "invite_db.json"

DEFAULT_RULES = """Rules:
1. No spam
2. No scams
3. No unsolicited DMs
4. Respect admins
5. Breaking rules gets you muted or banned
"""


# -------------------------
# JSON helpers
# -------------------------

def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_rules():
    return load_json(RULES_FILE, {})


def save_rules(data):
    save_json(RULES_FILE, data)


def get_rules(chat_id):
    data = load_rules()
    return data.get(str(chat_id), DEFAULT_RULES)


def set_rules(chat_id, rules):
    data = load_rules()
    data[str(chat_id)] = rules
    save_rules(data)


def load_db():
    db = load_json(DB_FILE, {
        "links": {},
        "counts": {},
        "joins": {},
        "users": {},
        "bans": {},
        "pending_verify": {},
        "verified": {},
        "message_counts": {},
        "link_review": {},
        "auto_approved": {},
        "public_invites": {},
        "link_guard_settings": {},
        "group_start_hits": {},
    })

    for key in [
        "links",
        "counts",
        "joins",
        "users",
        "bans",
        "pending_verify",
        "verified",
        "message_counts",
        "link_review",
        "auto_approved",
        "public_invites",
        "link_guard_settings",
        "group_start_hits",
    ]:
        if key not in db or not isinstance(db[key], dict):
            db[key] = {}

    return db


def save_db(db):
    save_json(DB_FILE, db)



def remember_user(user, source="unknown", chat_id=None, chat_title=None):
    db = load_db()
    key = str(user.id)

    existing = db["users"].get(key, {})

    servers = existing.get("servers", {})
    if chat_id is not None:
        servers[str(chat_id)] = {
            "chat_id": chat_id,
            "chat_title": chat_title or "unknown",
            "last_seen": int(time.time()),
            "source": source,
        }

    db["users"][key] = {
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "is_bot": user.is_bot,
        "last_source": source,
        "last_seen": int(time.time()),
        "servers": servers,
    }

    save_db(db)



async def set_member_tag(chat_id, user_id, tag):
    def _call():
        import json

        data = {
            "chat_id": str(chat_id),
            "user_id": str(user_id),
        }

        if tag is not None:
            data["tag"] = tag

        encoded = urllib.parse.urlencode(data).encode()
        url = f"https://api.telegram.org/bot{TOKEN}/setChatMemberTag"

        req = urllib.request.Request(url, data=encoded, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")
                print(f"[TAG OK] chat={chat_id} user={user_id} tag={tag} response={body}", flush=True)
                return True
        except Exception as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            print(
                f"[TAG ERROR] chat={chat_id} user={user_id} tag={tag} error={e} body={body}",
                flush=True
            )
            return False

    return await asyncio.to_thread(_call)



def add_pending_verify(chat_id, chat_title, user):
    db = load_db()
    db.setdefault("pending_verify", {})

    db["pending_verify"][str(user.id)] = {
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "chat_id": chat_id,
        "chat_title": chat_title or "unknown",
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 86400,
    }

    save_db(db)


def get_pending_verify(user_id):
    db = load_db()
    pending = db.get("pending_verify", {}).get(str(user_id))

    if not pending:
        return None

    if pending.get("expires_at", 0) < int(time.time()):
        db.get("pending_verify", {}).pop(str(user_id), None)
        save_db(db)
        return None

    return pending


def clear_pending_verify(user_id):
    db = load_db()
    db.get("pending_verify", {}).pop(str(user_id), None)
    save_db(db)


def complete_verification(user, chat_id, chat_title="unknown", source="verified"):
    """Move one user from pending/unverified to verified in one database write."""
    db = load_db()
    chat_key = str(chat_id)
    user_id = user.id if hasattr(user, "id") else int(user)
    user_key = str(user_id)
    existing_user = db.get("users", {}).get(user_key, {})
    now = int(time.time())

    db.setdefault("verified", {})
    db["verified"].setdefault(chat_key, {})
    db["verified"][chat_key][user_key] = {
        "user_id": user_id,
        "username": getattr(user, "username", None) or existing_user.get("username"),
        "name": getattr(user, "full_name", None) or existing_user.get("name") or f"User {user_id}",
        "chat_id": int(chat_id),
        "chat_title": chat_title or "unknown",
        "verified_at": now,
        "source": source,
    }

    pending = db.setdefault("pending_verify", {})
    pending_info = pending.get(user_key)
    if not pending_info or str(pending_info.get("chat_id")) == chat_key:
        pending.pop(user_key, None)

    db.setdefault("member_audit", {})
    audit = db["member_audit"].setdefault(chat_key, {
        "chat_id": int(chat_id),
        "title": chat_title or chat_key,
        "scanned_at": now,
        "members": {},
    })
    members = audit.setdefault("members", {})
    existing_member = members.get(user_key, {})
    members[user_key] = {
        **existing_member,
        "user_id": user_id,
        "username": getattr(user, "username", None) or existing_user.get("username"),
        "name": getattr(user, "full_name", None) or existing_user.get("name") or f"User {user_id}",
        "is_bot": bool(getattr(user, "is_bot", existing_user.get("is_bot", False))),
        "is_deleted": False,
        "is_admin": bool(existing_member.get("is_admin", False)),
        "verify_status": "verified",
        "in_verified_db": True,
        "in_pending_db": False,
        "updated_at": now,
    }

    save_db(db)


def mark_verified(user, chat_id, chat_title="unknown", source="verified"):
    db = load_db()
    db.setdefault("verified", {})
    chat_key = str(chat_id)
    user_id = user.id if hasattr(user, "id") else int(user)
    user_key = str(user_id)

    if chat_key not in db["verified"]:
        db["verified"][chat_key] = {}

    existing = db.get("users", {}).get(user_key, {})

    db["verified"][chat_key][user_key] = {
        "user_id": user_id,
        "username": getattr(user, "username", None) or existing.get("username"),
        "name": getattr(user, "full_name", None) or existing.get("name") or f"User {user_id}",
        "chat_id": int(chat_id),
        "chat_title": chat_title or "unknown",
        "verified_at": int(time.time()),
        "source": source,
    }

    save_db(db)


def is_verified(user_id, chat_id):
    db = load_db()
    return str(user_id) in db.get("verified", {}).get(str(chat_id), {})

def audit_set_user(chat_id, chat_title, user, status):
    db = load_db()
    db.setdefault("member_audit", {})
    chat_key = str(chat_id)

    if chat_key not in db["member_audit"]:
        db["member_audit"][chat_key] = {
            "chat_id": int(chat_id),
            "title": chat_title or str(chat_id),
            "scanned_at": int(time.time()),
            "members": {},
        }

    members = db["member_audit"][chat_key].setdefault("members", {})
    uid = str(user.id)

    existing = members.get(uid, {})

    members[uid] = {
        **existing,
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "is_bot": bool(user.is_bot),
        "is_deleted": False,
        "is_admin": bool(existing.get("is_admin", False)),
        "verify_status": status,
        "in_verified_db": status == "verified",
        "in_pending_db": status == "pending",
        "updated_at": int(time.time()),
    }

    save_db(db)


def audit_mark_verified(chat_id, chat_title, user):
    audit_set_user(chat_id, chat_title, user, "verified")


def audit_mark_pending(chat_id, chat_title, user):
    audit_set_user(chat_id, chat_title, user, "pending")


# -------------------------
# Permissions
# -------------------------

LOCKED_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

UNLOCKED_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


async def user_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ["administrator", "creator"]



def is_persist_banned(chat_id, user_id):
    db = load_db()
    return str(user_id) in db.get("bans", {}).get(str(chat_id), {})


def add_persist_ban(chat_id, user, banned_by, reason=""):
    db = load_db()
    chat_key = str(chat_id)

    if "bans" not in db:
        db["bans"] = {}

    if chat_key not in db["bans"]:
        db["bans"][chat_key] = {}

    db["bans"][chat_key][str(user.id)] = {
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "banned_by": banned_by.id,
        "banned_by_name": banned_by.full_name,
        "reason": reason,
        "banned_at": int(time.time()),
    }

    save_db(db)


def remove_persist_ban(chat_id, user_id):
    db = load_db()
    chat_key = str(chat_id)

    if "bans" in db and chat_key in db["bans"]:
        db["bans"][chat_key].pop(str(user_id), None)

    save_db(db)


# -------------------------
# Rules commands
# -------------------------

async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Use this inside the group.")
        return

    if not await user_is_admin(update, context):
        await update.message.reply_text("Only admins can change the rules.")
        return

    text = update.message.text or ""

    parts = text.split(maxsplit=1)
    new_rules = parts[1].strip() if len(parts) > 1 else ""

    if not new_rules and update.message.reply_to_message:
        new_rules = (
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        ).strip()

    if not new_rules:
        await update.message.reply_text(
            "Usage:\n\n"
            "/setrules Rule 1. Rule 2. Rule 3.\n\n"
            "Or reply to a message containing the rules and send:\n"
            "/setrules"
        )
        return

    set_rules(update.effective_chat.id, new_rules)

    saved = get_rules(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Rules updated.\n\n" + saved
    )


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = get_rules(update.effective_chat.id)
    await update.message.reply_text(rules)


# -------------------------
# Invite tracking commands
# -------------------------


async def send_private_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    user = update.effective_user

    if user.id != user_id:
        await update.message.reply_text("This invite request is not for you.")
        return

    try:
        chat_obj = await context.bot.get_chat(chat_id)
        chat_title = chat_obj.title
    except Exception:
        chat_title = "unknown"

    remember_user(
        user,
        "myinvite",
        chat_id=chat_id,
        chat_title=chat_title,
    )

    try:
        chat_obj = await context.bot.get_chat(chat_id)
        chat_title = chat_obj.title
    except Exception:
        chat_title = "unknown"

    remember_user(
        user,
        "myinvite",
        chat_id=chat_id,
        chat_title=chat_title,
    )

    db = load_db()
    key = f"{chat_id}:{user.id}"

    if key in db["links"]:
        pending_key = f"pending_invite_msg:{chat_id}:{user.id}"

        if pending_key in db:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=db[pending_key]
                )
            except Exception:
                pass

            db.pop(pending_key, None)
            save_db(db)

        await update.message.reply_text(
            f"Your personal invite link:\n{db['links'][key]['link']}"
        )
        return

    invite = await context.bot.create_chat_invite_link(
        chat_id=chat_id,
        name=f"{user.full_name} / {user.id}",
        creates_join_request=False,
    )

    db["links"][key] = {
        "chat_id": chat_id,
        "chat_title": "unknown",
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "link": invite.invite_link,
        "created_at": int(time.time()),
    }

    db["counts"][key] = db["counts"].get(key, 0)
    save_db(db)

    db = load_db()
    pending_key = f"pending_invite_msg:{chat_id}:{user.id}"

    if pending_key in db:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=db[pending_key]
            )
        except Exception:
            pass

        db.pop(pending_key, None)
        save_db(db)

    await update.message.reply_text(
        f"Your personal invite link:\n{invite.invite_link}"
    )


async def myinvite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Use this inside the group.")
        return

    # Delete the user's /myinvite request from the group
    try:
        await update.message.delete()
    except Exception:
        pass

    bot_info = await context.bot.get_me()
    payload = make_invite_payload(chat.id, user.id)
    start_url = f"https://t.me/{bot_info.username}?start={payload}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Get your invite link", url=start_url)]
    ])

    msg = await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"{user.mention_html()} I deleted your invite request.\n\n"
            f"Click below and I’ll give you your personal invite link privately."
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    db = load_db()
    db[f"pending_invite_msg:{chat.id}:{user.id}"] = msg.message_id
    save_db(db)


async def myinvites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    db = load_db()
    key = f"{chat.id}:{user.id}"
    count = db["counts"].get(key, 0)

    await update.message.reply_text(f"You have invited {count} member(s).")


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    db = load_db()

    rows = []

    for key, count in db["counts"].items():
        chat_id, user_id = key.split(":")
        if int(chat_id) != chat.id:
            continue

        info = db["links"].get(key, {})
        name = info.get("name", "Unknown")
        username = info.get("username")

        label = f"@{username}" if username else name
        rows.append((label, count))

    if not rows:
        await update.message.reply_text("No invites tracked yet.")
        return

    rows.sort(key=lambda x: x[1], reverse=True)

    text = "🏆 Invite Leaderboard\n\n"
    for i, (name, count) in enumerate(rows[:10], start=1):
        text += f"{i}. {name} — {count}\n"

    await update.message.reply_text(text)


# -------------------------
# Join gate + invite count
# -------------------------

async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg is None:
        return

    if not getattr(msg, "new_chat_members", None):
        return

    chat = update.effective_chat
    db = load_db()

    used_invite = None
    msg_dict = msg.to_dict()
    invite_obj = msg_dict.get("invite_link") or msg_dict.get("chat_invite_link")
    if isinstance(invite_obj, dict):
        used_invite = invite_obj.get("invite_link")

    for user in msg.new_chat_members:
        if user.is_bot:
            continue

        # Persistent ban check
        if is_persist_banned(chat.id, user.id):
            try:
                await context.bot.ban_chat_member(
                    chat_id=chat.id,
                    user_id=user.id,
                )
                await msg.reply_text(
                    f"🔨 {user.mention_html()} was on the persistent ban list and was removed.",
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"[PERSIST BAN ERROR] chat={chat.id} user={user.id} error={e}", flush=True)
            continue

        # Store user, server, and join time going forward
        now = int(time.time())
        db = load_db()
        uid = str(user.id)
        existing = db.get("users", {}).get(uid, {})
        servers = existing.get("servers", {})
        server_key = str(chat.id)
        server_existing = servers.get(server_key, {})
        server_existing.update({
            "chat_id": chat.id,
            "chat_title": chat.title or "unknown",
            "last_seen": now,
            "source": "join",
        })
        server_existing.setdefault("joined_at", now)
        server_existing.setdefault("join_source", "new_member_event")
        servers[server_key] = server_existing
        db.setdefault("users", {})
        db["users"][uid] = {
            **existing,
            "user_id": user.id,
            "username": user.username,
            "name": user.full_name,
            "is_bot": user.is_bot,
            "last_source": "join",
            "last_seen": now,
            "servers": servers,
        }

        # Track invite usage
        if used_invite:
            for inviter_key, info in db.get("links", {}).items():
                if info.get("link") == used_invite:
                    join_key = f"{chat.id}:{user.id}"

                    if join_key not in db.get("joins", {}):
                        db.setdefault("counts", {})
                        db.setdefault("joins", {})
                        db["counts"][inviter_key] = db["counts"].get(inviter_key, 0) + 1
                        db["joins"][join_key] = {
                            "chat_id": chat.id,
                            "joined_user_id": user.id,
                            "joined_name": user.full_name,
                            "joined_username": user.username,
                            "invited_by": inviter_key,
                            "joined_at": now,
                        }
                    break

        save_db(db)

        # Add pending verification so the shared /verificationbutton can work
        add_pending_verify(chat.id, chat.title, user)
        audit_mark_pending(chat.id, chat.title, user)

        # Add UNVERIFIED member tag
        await set_member_tag(chat.id, user.id, "UNVERIFIED")

        # Mute new member
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=LOCKED_PERMS,
            )
        except Exception as e:
            print(f"[JOIN MUTE ERROR] chat={chat.id} user={user.id} error={e}", flush=True)

        # Short public notice with verification button
        try:
            bot_info = await context.bot.get_me()
            verify_url = f"https://t.me/{bot_info.username}?start=verify"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Verify Access", url=verify_url)]
            ])

            await msg.reply_text(
                f"🔒 {user.mention_html()} is unverified and muted.\n\n"
                f"Click below, DM the bot, read the rules, and verify to unlock chat.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            pass


async def agree_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    remember_user(query.from_user, "agree")

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("This verification button is invalid.", show_alert=True)
        return

    _, chat_id, target_user_id = parts
    chat_id = int(chat_id)
    target_user_id = int(target_user_id)

    if query.from_user.id != target_user_id:
        await query.answer("This button is not for you.", show_alert=True)
        return

    already_verified = is_verified(target_user_id, chat_id)

    try:
        chat_obj = await context.bot.get_chat(chat_id)
        chat_title = chat_obj.title
    except Exception:
        chat_title = "unknown"

    remember_user(
        query.from_user,
        "agree",
        chat_id=chat_id,
        chat_title=chat_title,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=UNLOCKED_PERMS,
        )
    except Exception as e:
        await query.answer("Verification failed. Please try again.", show_alert=True)
        await query.edit_message_text(f"❌ Failed to unlock you: {e}")
        return

    await set_member_tag(chat_id, target_user_id, None)
    complete_verification(
        query.from_user,
        chat_id,
        chat_title,
        source="rules_agree",
    )

    try:
        if already_verified:
            await query.answer(
                "You have already verified. Your access is confirmed.",
                show_alert=True,
            )
            await query.edit_message_text(
                f"✅ Already verified for {chat_title}.\n\n"
                "Your access is confirmed and you can return to the server."
            )
        else:
            await query.answer(
                "Verification complete! You are now verified.",
                show_alert=True,
            )
            await query.edit_message_text(
                f"✅ You are verified for {chat_title}.\n\n"
                "Your access is unlocked and you can return to the server."
            )
    except Exception:
        try:
            await query.edit_message_text(
                "✅ Verification complete. Your server access is unlocked."
            )
        except Exception:
            pass


# -------------------------
# Basic commands
# -------------------------


def make_start_payload(chat_id, user_id):
    chat_part = str(chat_id).replace("-", "m")
    return f"gate_{chat_part}_{user_id}"


def parse_start_payload(payload):
    if not payload.startswith("gate_"):
        return None, None

    parts = payload.split("_")
    if len(parts) != 3:
        return None, None

    chat_part = parts[1].replace("m", "-")
    user_part = parts[2]

    try:
        return int(chat_part), int(user_part)
    except Exception:
        return None, None


def make_invite_payload(chat_id, user_id):
    chat_part = str(chat_id).replace("-", "m")
    return f"invite_{chat_part}_{user_id}"


def parse_invite_payload(payload):
    if not payload.startswith("invite_"):
        return None, None

    parts = payload.split("_")
    if len(parts) != 3:
        return None, None

    chat_part = parts[1].replace("m", "-")
    user_part = parts[2]

    try:
        return int(chat_part), int(user_part)
    except Exception:
        return None, None

async def auto_approve_main_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is a safety net for users who were muted before pending_verify existed.
    # It does NOT unlock immediately. It creates a pending verification record and sends the menu.
    user = update.effective_user
    chat_id = -1003907893676

    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if str(member.status).lower().endswith("restricted") or member.status == "restricted":
            try:
                chat_obj = await context.bot.get_chat(chat_id)
                chat_title = chat_obj.title
            except Exception:
                chat_title = "the server"

            add_pending_verify(chat_id, chat_title, user)
            remember_user(
                user,
                "auto_pending_dm",
                chat_id=chat_id,
                chat_title=chat_title,
            )
            return True

    except Exception as e:
        print(f"[AUTO PENDING ERROR] user={user.id} error={e}", flush=True)

    return False


async def private_touch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignore random private messages. Verification should happen through /start.
    return
async def self_heal_verify(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    # Servers to check when pending_verify is missing or stale
    SERVER_IDS = [-1003907893676, -1003968113195]

    for chat_id in SERVER_IDS:
        try:
            member = await context.bot.get_chat_member(chat_id, user.id)
            status = str(member.status).lower()

            try:
                chat_obj = await context.bot.get_chat(chat_id)
                chat_title = chat_obj.title or str(chat_id)
            except Exception:
                chat_title = str(chat_id)

            if "restricted" in status:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user.id,
                    permissions=UNLOCKED_PERMS,
                )

                await set_member_tag(chat_id, user.id, None)
                complete_verification(
                    user,
                    chat_id,
                    chat_title,
                    source="self_heal_verified",
                )

                remember_user(
                    user,
                    "self_heal_verified",
                    chat_id=chat_id,
                    chat_title=chat_title,
                )

                return True, f"✅ You were found muted in {chat_title} and have been unlocked."

            if status in ["member", "administrator", "creator"]:
                remember_user(
                    user,
                    "self_heal_already_verified",
                    chat_id=chat_id,
                    chat_title=chat_title,
                )

                return True, f"✅ You are already approved in {chat_title}."

        except Exception:
            continue

    return False, (
        "I could not find you as muted or approved in the server.\n\n"
        "Make sure you joined the correct server with this same Telegram account."
    )


async def send_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Read Rules", callback_data="menu_rules")],
        [InlineKeyboardButton("✅ Verify Access", callback_data="menu_verify")],
        [InlineKeyboardButton("🔗 Get Invite", callback_data="menu_invites")],
    ])

    await update.message.reply_text(
        "🤖 Verification Portal\n\n"
        "Welcome.\n\n"
        "Please read the rules, then verify access.",
        reply_markup=keyboard,
    )


async def get_verification_candidates(context, user):
    """Return active servers where the user is not yet verified internally."""
    db = load_db()
    user_key = str(user.id)
    server_ids = set(load_rules().keys())

    stored_user = db.get("users", {}).get(user_key, {})
    server_ids.update(stored_user.get("servers", {}).keys())

    for server_id, audit in db.get("member_audit", {}).items():
        if user_key in audit.get("members", {}):
            server_ids.add(server_id)

    candidates = []
    for server_id in server_ids:
        try:
            chat_id = int(server_id)
        except (TypeError, ValueError):
            continue

        if is_verified(user.id, chat_id):
            continue

        try:
            member = await context.bot.get_chat_member(chat_id, user.id)
            status = str(member.status).lower()
            if status not in ["member", "administrator", "creator", "restricted"]:
                continue

            chat = await context.bot.get_chat(chat_id)
            candidates.append({
                "chat_id": chat_id,
                "chat_title": chat.title or str(chat_id),
                "status": status,
            })
        except Exception:
            continue

    return sorted(candidates, key=lambda item: item["chat_title"].lower())


async def show_verification_prompt(query, user, chat_id, chat_title):
    rules = get_rules(chat_id)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ I agree to the rules",
                callback_data=f"agree:{chat_id}:{user.id}",
            )
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_back")],
    ])

    await query.edit_message_text(
        f"✅ Verify Access for {chat_title}\n\n"
        "Please read and acknowledge the rules below.\n\n"
        f"{rules}",
        reply_markup=keyboard,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    if data == "menu_rules":
        db = load_db()
        rules_data = load_rules()

        keyboard = []

        for sid, rules_text in rules_data.items():
            try:
                chat_obj = await context.bot.get_chat(int(sid))
                title = chat_obj.title or sid
            except Exception:
                title = sid

            keyboard.append([
                InlineKeyboardButton(
                    f"📜 {title}",
                    callback_data=f"menu_rules_show:{sid}"
                )
            ])

        if not keyboard:
            await query.edit_message_text(
                "No server rules are saved right now.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_back")]
                ])
            )
            return

        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_back")])

        await query.edit_message_text(
            "📜 Choose which server rules to read:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("menu_rules_show:"):
        sid = data.split(":", 1)[1]
        rules = get_rules(int(sid))

        try:
            chat_obj = await context.bot.get_chat(int(sid))
            title = chat_obj.title or sid
        except Exception:
            title = sid

        await query.edit_message_text(
            f"📜 Rules for {title}\n\n{rules}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back", callback_data="menu_rules")]
            ])
        )
        return

    if data == "menu_verify":
        pending = get_pending_verify(user.id)

        if not pending:
            db = load_db()
            candidates = await get_verification_candidates(context, user)
            verified_servers = [
                info
                for members in db.get("verified", {}).values()
                for uid, info in members.items()
                if str(uid) == str(user.id)
            ]

            if len(candidates) == 1:
                candidate = candidates[0]
                add_pending_verify(
                    candidate["chat_id"],
                    candidate["chat_title"],
                    user,
                )
                await show_verification_prompt(
                    query,
                    user,
                    candidate["chat_id"],
                    candidate["chat_title"],
                )
            elif len(candidates) > 1:
                keyboard = [
                    [
                        InlineKeyboardButton(
                            f"✅ {candidate['chat_title']}",
                            callback_data=f"menu_verify_server:{candidate['chat_id']}",
                        )
                    ]
                    for candidate in candidates
                ]
                keyboard.append([
                    InlineKeyboardButton("⬅️ Back", callback_data="menu_back")
                ])
                await query.edit_message_text(
                    "Choose the server you want to verify for:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            elif verified_servers:
                server_names = sorted({
                    info.get("chat_title") or str(info.get("chat_id", "the server"))
                    for info in verified_servers
                })
                await query.edit_message_text(
                    "✅ You have already verified.\n\n"
                    f"Verified server(s): {', '.join(server_names)}\n\n"
                    "Your access is already unlocked."
                )
            else:
                await query.edit_message_text(
                    "I do not see a pending verification request for you right now.\n\n"
                    "If you cannot talk in the server, contact an admin so they can restore your verification request."
                )
            return

        chat_id = int(pending["chat_id"])
        chat_title = pending.get("chat_title", "the server")
        await show_verification_prompt(query, user, chat_id, chat_title)
        return

    if data.startswith("menu_verify_server:"):
        try:
            chat_id = int(data.split(":", 1)[1])
            member = await context.bot.get_chat_member(chat_id, user.id)
            status = str(member.status).lower()
            if status not in ["member", "administrator", "creator", "restricted"]:
                await query.edit_message_text(
                    "I could not confirm that you are currently in that server."
                )
                return

            if is_verified(user.id, chat_id):
                await query.edit_message_text(
                    "✅ You have already verified for this server.\n\n"
                    "Your access is already unlocked."
                )
                return

            chat = await context.bot.get_chat(chat_id)
            chat_title = chat.title or str(chat_id)
            add_pending_verify(chat_id, chat_title, user)
            await show_verification_prompt(query, user, chat_id, chat_title)
        except Exception:
            await query.edit_message_text(
                "I could not prepare verification for that server. Please try again."
            )
        return


    if data == "menu_invites":
        db = load_db()
        public_invites = db.get("public_invites",
        "link_guard_settings",
        "group_start_hits", {})

        available = []

        for sid, info in public_invites.items():
            if not info.get("enabled") or not info.get("invite_link"):
                continue

            server_id = int(info.get("server_id") or sid)

            try:
                member = await context.bot.get_chat_member(server_id, user.id)
                status = str(member.status).lower()

                # Only hand out invite links for servers the user is already in.
                if status in ["member", "administrator", "creator", "restricted"]:
                    available.append((sid, info))

            except Exception:
                # If the bot cannot confirm they are in that server, do NOT show the invite.
                continue

        if not available:
            await query.edit_message_text(
                "🔗 No invite links are available for you right now.\n\n"
                "Invite links are only shown for servers you are already a member of.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu_back")]
                ])
            )
            return

        keyboard = []
        for sid, info in available:
            keyboard.append([
                InlineKeyboardButton(
                    f"🔗 {info.get('title', sid)}",
                    callback_data=f"menu_invitecopy:{sid}"
                )
            ])

        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_back")])

        await query.edit_message_text(
            "🔗 Invite Links\n\n"
            "These are invite links for servers you are already in. You may share them with friends.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("menu_invitecopy:"):
        sid = data.split(":", 1)[1]

        db = load_db()
        info = db.get("public_invites",
        "link_guard_settings",
        "group_start_hits", {}).get(str(sid))

        if not info or not info.get("enabled") or not info.get("invite_link"):
            await query.edit_message_text("That invite link is not available right now.")
            return

        await query.message.reply_text(
            f"🔗 Invite link for {info.get('title', sid)}:\n\n"
            f"{info.get('invite_link')}\n\n"
            "You can copy or forward this message to a friend."
        )

        await query.answer("Invite link sent below.")
        return

    if data == "menu_status":
        db = load_db()
        pending = get_pending_verify(user.id)
        user_info = db.get("users", {}).get(str(user.id), {})
        servers = user_info.get("servers", {})

        status = "⏳ Pending verification" if pending else "✅ No pending verification"

        text = f"📊 My Status\n\nStatus: {status}\n\n"

        if servers:
            text += "Servers stored:\n"
            for sid, sinfo in servers.items():
                text += f"• {sinfo.get('chat_title', 'unknown')} — {sid}\n"
        else:
            text += "No stored servers found for your account.\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="menu_back")]
        ])

        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data == "menu_back":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 Read Rules", callback_data="menu_rules")],
            [InlineKeyboardButton("✅ Verify Access", callback_data="menu_verify")],
        ])

        await query.edit_message_text(
            "🤖 Verification Portal\n\n"
            "Please read the rules, then verify access.",
            reply_markup=keyboard,
        )
        return

async def group_start_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return True

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return True

    if chat.type not in ["group", "supergroup"]:
        return False

    # Delete /start from group
    try:
        await update.message.delete()
    except Exception:
        pass

    # Ignore admins for punishment, but still delete the command
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ["administrator", "creator"]:
            return True
    except Exception:
        member = None

    now = int(time.time())
    db = load_db()
    db.setdefault("group_start_hits", {})

    key = f"{chat.id}:{user.id}"
    hits = [
        t for t in db["group_start_hits"].get(key, [])
        if now - int(t) <= 180
    ]
    hits.append(now)
    db["group_start_hits"][key] = hits
    save_db(db)

    mention = user.mention_html()

    if len(hits) >= 3:
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=LOCKED_PERMS,
                until_date=now + 600,
            )
        except Exception as e:
            print(f"[GROUP START MUTE ERROR] chat={chat.id} user={user.id} error={e}", flush=True)

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"{mention} please stop posting /start in the group. "
                    f"You have been muted for 10 minutes."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        return True

    pending = get_pending_verify(user.id)
    verified = is_verified(user.id, chat.id)

    status_text = None

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        member_status = str(member.status).lower()
    except Exception:
        member_status = ""

    if verified:
        status_text = (
            f"{mention} you are already verified. "
            f"If you can talk in the server, you are good."
        )
    elif pending and int(pending.get("chat_id", 0)) == chat.id:
        status_text = (
            f"{mention} you still need to verify. "
            f"Please DM the bot, press /start, read the rules, and click Verify Access."
        )
    elif "restricted" in member_status:
        status_text = (
            f"{mention} you appear to be muted. "
            f"Please DM the bot, press /start, and complete verification."
        )
    else:
        status_text = (
            f"{mention} if you can already talk in the server, you are verified. "
            f"If you cannot talk, DM the bot and press /start."
        )

    try:
        msg = await context.bot.send_message(
            chat_id=chat.id,
            text=status_text,
            parse_mode="HTML",
        )

        # Auto-clean the helper response after 30 seconds
        async def delete_later():
            await asyncio.sleep(30)
            try:
                await msg.delete()
            except Exception:
                pass

        asyncio.create_task(delete_later())

    except Exception:
        pass

    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if await group_start_guard(update, context):
        return

    remember_user(update.effective_user, "start")

    # Keep private invite-link flow working
    if context.args:
        payload = context.args[0]

        invite_chat_id, invite_user_id = parse_invite_payload(payload)
        if invite_chat_id and invite_user_id:
            await send_private_invite_link(update, context, invite_chat_id, invite_user_id)
            return

        chat_id, target_user_id = parse_start_payload(payload)
        if chat_id and target_user_id:
            if update.effective_user.id != target_user_id:
                await update.message.reply_text("This verification link is not for you.")
                return

            try:
                chat_obj = await context.bot.get_chat(chat_id)
                chat_title = chat_obj.title
            except Exception:
                chat_title = "unknown"

            remember_user(
                update.effective_user,
                "verify_start",
                chat_id=chat_id,
                chat_title=chat_title,
            )

            add_pending_verify(chat_id, chat_title, update.effective_user)

    # Safety net: if user is restricted in the main group but has no pending record, create one.
    if not get_pending_verify(update.effective_user.id):
        await auto_approve_main_group(update, context)

    # Only show the full menu when explicitly requested
    if context.args and context.args[0].lower() in ["verify", "menu"]:
        await send_start_menu(update, context)
        return

    await update.message.reply_text(
        "DM received.\n\n"
        "To verify, send:\n/start verify"
    )


async def verificationbutton_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    bot_info = await context.bot.get_me()
    url = f"https://t.me/{bot_info.username}?start=verify"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Verify Access", url=url)]
    ])

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id if update.message else None,
        text=(
            "🔒 Verification and Acknowledgement of Rules Required\n\n"
            "Click the button below to message the bot with /start to verify."
        ),
        reply_markup=keyboard,
    )


async def repairpending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Use this inside the group.")
        return

    if not await user_is_admin(update, context):
        await update.message.reply_text("Only admins can repair pending verification.")
        return

    chat = update.effective_chat
    db = load_db()
    users = db.get("users", {})

    checked = 0
    repaired = 0
    skipped = 0
    failed = 0

    await update.message.reply_text("Checking stored users for muted/unverified members...")

    for uid, info in users.items():
        servers = info.get("servers", {})

        if str(chat.id) not in servers:
            skipped += 1
            continue

        try:
            member = await context.bot.get_chat_member(chat.id, int(uid))
            checked += 1

            if member.status == "restricted":
                class TempUser:
                    pass

                u = TempUser()
                u.id = int(uid)
                u.username = info.get("username")
                u.full_name = info.get("name") or f"User {uid}"

                add_pending_verify(chat.id, chat.title, u)
                repaired += 1

        except Exception:
            failed += 1

    await update.message.reply_text(
        "✅ Pending verification repair complete.\n\n"
        f"Checked: {checked}\n"
        f"Repaired pending: {repaired}\n"
        f"Skipped wrong server: {skipped}\n"
        f"Failed: {failed}"
    )

def message_has_link(message):
    text = message.text or message.caption or ""

    if message.entities:
        for e in message.entities:
            if e.type in ["url", "text_link"]:
                return True

    if message.caption_entities:
        for e in message.caption_entities:
            if e.type in ["url", "text_link"]:
                return True

    patterns = [
        r"https?://",
        r"www\.",
        r"t\.me/",
        r"telegram\.me/",
        r"discord\.gg/",
        r"\.com\b",
        r"\.net\b",
        r"\.org\b",
        r"\.io\b",
    ]

    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


async def early_link_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type not in ["group", "supergroup"]:
        return

    if user.is_bot:
        return

    # ignore admins
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ["administrator", "creator"]:
            return
    except Exception:
        pass

    db = load_db()
    db.setdefault("message_counts", {})
    db.setdefault("link_review", {})

    key = f"{chat.id}:{user.id}"
    count = db["message_counts"].get(key, 0) + 1
    db["message_counts"][key] = count
    save_db(db)

    settings = db.get("link_guard_settings",
        "group_start_hits", {}).get(str(chat.id), {})
    enabled = settings.get("enabled", True)
    max_messages = int(settings.get("max_messages", 2))

    if not enabled:
        return

    # Only guard configured number of first posts
    if count > max_messages:
        return

    if not message_has_link(update.message):
        return

    # Delete link message
    try:
        await update.message.delete()
    except Exception:
        pass

    # Mute user until admin review
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=LOCKED_PERMS,
        )
    except Exception as e:
        print(f"[LINK GUARD MUTE ERROR] chat={chat.id} user={user.id} error={e}", flush=True)

    db = load_db()
    db.setdefault("link_review", {})
    db["link_review"][key] = {
        "chat_id": chat.id,
        "chat_title": chat.title,
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "message_count": count,
        "text": update.message.text or update.message.caption or "",
        "created_at": int(time.time()),
    }
    save_db(db)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"linkapprove:{chat.id}:{user.id}"),
            InlineKeyboardButton("🔨 Ban", callback_data=f"linkban:{chat.id}:{user.id}"),
        ]
    ])

    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"🚨 Link guard triggered\n\n"
            f"User: {user.mention_html()}\n"
            f"ID: <code>{user.id}</code>\n"
            f"Message #: {count}\n\n"
            f"Their message was deleted and they were muted pending admin review."
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("⏳ Pending", callback_data="admin_pending"),
        ],
        [
            InlineKeyboardButton("🔒 Verify Button", callback_data="admin_verifybutton"),
            InlineKeyboardButton("📣 Reminder Settings", callback_data="admin_remind"),
        ],
        [
            InlineKeyboardButton("⚙️ Reminder Status", callback_data="admin_reminderstatus"),
            InlineKeyboardButton("🆔 Server ID", callback_data="admin_serverid"),
        ],
        [
            InlineKeyboardButton("🔨 Ban User", callback_data="admin_banhelp"),
            InlineKeyboardButton("🔗 Invite Settings", callback_data="admin_invitesettings"),
        ],
        [
            InlineKeyboardButton("📜 Rules Settings", callback_data="admin_rulesettings"),
            InlineKeyboardButton("🔗 Link Guard", callback_data="admin_linkguard"),
        ],
    ])

    # If used in group, delete command and send admin menu to DM
    if update.effective_chat.type in ["group", "supergroup"]:
        group_chat = update.effective_chat

        if not await user_is_admin(update, context):
            return

        try:
            await update.message.delete()
        except Exception:
            pass

        context.user_data["admin_server_id"] = group_chat.id

        try:
            await update.effective_user.send_message(
                f"🛠 Admin Menu\n\nActive server:\n{group_chat.title}\n{group_chat.id}\n\nChoose an option below:",
                reply_markup=keyboard,
            )
        except Exception:
            await context.bot.send_message(
                chat_id=group_chat.id,
                text=(
                    f"{update.effective_user.mention_html()} start a DM with me first, "
                    f"then run /admin again."
                ),
                parse_mode="HTML",
            )
        return

    # If used in DM, require /admin SERVER_ID unless one is already stored
    if update.effective_chat.type == "private":
        if context.args:
            try:
                server_id = int(context.args[0])
            except Exception:
                await update.message.reply_text("SERVER_ID must be a number.")
                return

            try:
                member = await context.bot.get_chat_member(server_id, update.effective_user.id)
                if member.status not in ["administrator", "creator"]:
                    await update.message.reply_text("Admins only.")
                    return
            except Exception as e:
                await update.message.reply_text(f"Could not verify admin status: {e}")
                return

            context.user_data["admin_server_id"] = server_id

        server_id = context.user_data.get("admin_server_id")
        if not server_id:
            await update.message.reply_text(
                "Use /admin inside the group first, or use:\n\n/admin SERVER_ID"
            )
            return

        try:
            chat = await context.bot.get_chat(server_id)
            title = chat.title
        except Exception:
            title = str(server_id)

        await update.message.reply_text(
            f"🛠 Admin Menu\n\nActive server:\n{title}\n{server_id}\n\nChoose an option below:",
            reply_markup=keyboard,
        )
        return


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    print(f"[ADMIN CALLBACK HIT] user={query.from_user.id} data={query.data}", flush=True)
    await query.answer()

    user = query.from_user
    server_id = context.user_data.get("admin_server_id")

    if not server_id:
        await query.edit_message_text("Admin server not set. Run /admin in the group again.")
        return

    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(int(server_id), user.id),
            timeout=8
        )
        if member.status not in ["administrator", "creator"]:
            await query.edit_message_text("Admins only.")
            return
    except Exception as e:
        await query.edit_message_text(f"Could not verify admin status: {e}")
        return

    try:
        chat = await asyncio.wait_for(
            context.bot.get_chat(int(server_id)),
            timeout=8
        )
    except Exception as e:
        await query.edit_message_text(f"Could not load server: {e}")
        return

    db = load_db()
    data = query.data

    back = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
    ])

    if data == "admin_stats":
        stats = await get_readonly_stats(context.bot, db, chat.id)

        bans_here = db.get("bans", {}).get(str(chat.id), {})

        links_here = [
            info for info in db.get("links", {}).values()
            if str(info.get("chat_id")) == str(chat.id)
        ]

        reviews_here = [
            info for info in db.get("link_review", {}).values()
            if int(info.get("chat_id", 0)) == chat.id
        ]

        text = format_readonly_stats(
            chat.title,
            stats,
            len(links_here),
            len(bans_here),
            len(reviews_here),
        )

        await query.edit_message_text(text, reply_markup=back)
        return

    if data == "admin_pending":
        pending_here = [
            (uid, info)
            for uid, info in db.get("pending_verify", {}).items()
            if int(info.get("chat_id", 0)) == chat.id
        ]

        text = f"⏳ Pending verification records: {len(pending_here)}\n\n"

        for uid, info in pending_here[:40]:
            username = info.get("username")
            name = info.get("name") or f"User {uid}"
            label = f"@{username}" if username else name
            text += f"• {label} — {uid}\n"

        if len(pending_here) > 40:
            text += f"\n...and {len(pending_here) - 40} more."

        await query.edit_message_text(text, reply_markup=back)
        return

    if data == "admin_verifybutton":
        bot_info = await context.bot.get_me()
        url = f"https://t.me/{bot_info.username}?start=verify"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Verify Access", url=url)],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")],
        ])

        await query.edit_message_text(
            "🔒 Verification Button\n\n"
            "This button is shown here in DM only.\n\n"
            "Forward this DM to someone if needed, or tell users to DM the bot and press /start.",
            reply_markup=keyboard,
        )
        return

    if data == "admin_remind":
        settings = db.get("verify_reminders", {}).get(str(chat.id), {})
        pending_count = len([
            1 for pinfo in db.get("pending_verify", {}).values()
            if int(pinfo.get("chat_id", 0)) == chat.id
        ])

        text = (
            "📣 Reminder Settings\n\n"
            f"Server: {chat.title}\n"
            f"ID: {chat.id}\n\n"
            f"Reminder channel: {settings.get('reminder_chat_id', 'not set')}\n"
            f"Topic ID: {settings.get('message_thread_id', 'not set')}\n"
            f"Interval: {settings.get('interval_minutes', 60)} minutes\n"
            f"People per reminder: {settings.get('batch_size', 10)}\n"
            f"Daytime only: {settings.get('quiet_end_hour', 8)}:00 to {settings.get('quiet_start_hour', 21)}:00\n"
            f"Pending records: {pending_count}\n\n"
            "To set the reminder channel/topic, go to that topic and type:\n"
            "/setreminder"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("15 min", callback_data="admin_remind_interval:15"),
                InlineKeyboardButton("30 min", callback_data="admin_remind_interval:30"),
                InlineKeyboardButton("60 min", callback_data="admin_remind_interval:60"),
            ],
            [
                InlineKeyboardButton("5 people", callback_data="admin_remind_batch:5"),
                InlineKeyboardButton("10 people", callback_data="admin_remind_batch:10"),
                InlineKeyboardButton("25 people", callback_data="admin_remind_batch:25"),
            ],
            [
                InlineKeyboardButton("📣 Send Now", callback_data="admin_remind_sendnow"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="admin_back"),
            ],
        ])

        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("admin_remind_interval:"):
        minutes = int(data.split(":", 1)[1])

        db.setdefault("verify_reminders", {})
        settings = db["verify_reminders"].setdefault(str(chat.id), {})
        settings["server_id"] = chat.id
        settings["interval_minutes"] = minutes
        settings.setdefault("batch_size", 10)
        settings.setdefault("index", 0)
        settings.setdefault("last_run", 0)
        db["verify_reminders"][str(chat.id)] = settings
        save_db(db)

        query.data = "admin_remind"
        await admin_callback(update, context)
        return

    if data.startswith("admin_remind_batch:"):
        batch_size = int(data.split(":", 1)[1])

        db.setdefault("verify_reminders", {})
        settings = db["verify_reminders"].setdefault(str(chat.id), {})
        settings["server_id"] = chat.id
        settings["batch_size"] = batch_size
        settings.setdefault("interval_minutes", 60)
        settings.setdefault("index", 0)
        settings.setdefault("last_run", 0)
        db["verify_reminders"][str(chat.id)] = settings
        save_db(db)

        query.data = "admin_remind"
        await admin_callback(update, context)
        return

    if data == "admin_remind_sendnow":
        try:
            await post_pending_reminders(context, chat.id)
            await query.answer("Reminder batch sent.", show_alert=True)
        except Exception as e:
            await query.answer(f"Reminder failed: {e}", show_alert=True)
        return

    if data == "admin_reminderstatus":
        settings = db.get("verify_reminders", {}).get(str(chat.id))
        pending_count = len([
            1 for pinfo in db.get("pending_verify", {}).values()
            if int(pinfo.get("chat_id", 0)) == chat.id
        ])

        if not settings:
            text = (
                "⚙️ Reminder Status\n\n"
                "No reminder channel is configured for this server.\n\n"
                f"Pending records: {pending_count}\n\n"
                "Use:\n/setreminderchannel SERVER_ID"
            )
        else:
            text = (
                "⚙️ Reminder Status\n\n"
                f"Reminder chat: {settings.get('reminder_chat_id')}\n"
                f"Topic ID: {settings.get('message_thread_id')}\n"
                f"Interval: {settings.get('interval_minutes', 60)} minutes\n"
                f"Batch size: {settings.get('batch_size', 10)}\n"
                f"Pending records: {pending_count}"
            )

        await query.edit_message_text(text, reply_markup=back)
        return

    if data == "admin_banhelp":
        await query.edit_message_text(
            "🔨 Ban User\n\n"
            "From the group:\n"
            "Reply to a user's message with:\n"
            "/ban reason here\n\n"
            "By Telegram user ID:\n"
            "/ban USER_ID reason here\n\n"
            "Example:\n"
            "/ban 7634600277 spam/scam",
            reply_markup=back,
        )
        return

    if data == "admin_rulesettings":
        rules = get_rules(chat.id)

        text = (
            "📜 Rules Settings\n\n"
            f"Server: {chat.title}\n"
            f"ID: {chat.id}\n\n"
            "Current rules:\n\n"
            f"{rules}\n\n"
            "To change rules, send:\n\n"
            "/setrules NEW RULE TEXT HERE"
        )

        if len(text) > 3900:
            text = (
                "📜 Rules Settings\n\n"
                f"Server: {chat.title}\n"
                f"ID: {chat.id}\n\n"
                "Rules are too long to preview here.\n\n"
                "To change rules, send:\n\n"
                "/setrules NEW RULE TEXT HERE"
            )

        await query.edit_message_text(text, reply_markup=back)
        return

    if data == "admin_linkguard":
        db.setdefault("link_guard_settings",
        "group_start_hits", {})
        settings = db["link_guard_settings"].get(str(chat.id), {})
        enabled = settings.get("enabled", True)
        max_messages = int(settings.get("max_messages", 2))

        review_count = len([
            1 for item in db.get("link_review", {}).values()
            if int(item.get("chat_id", 0)) == chat.id
        ])

        text = (
            "🔗 Link Guard Settings\n\n"
            f"Server: {chat.title}\n"
            f"Status: {'ON ✅' if enabled else 'OFF ❌'}\n"
            f"Tracked first messages: {max_messages}\n"
            f"Review queue: {review_count}\n\n"
            "When ON, if a non-admin posts a link during their tracked first messages, "
            "the bot deletes the message, mutes the user, and saves the message for admin review."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Turn ON", callback_data="admin_linkguard_on"),
                InlineKeyboardButton("❌ Turn OFF", callback_data="admin_linkguard_off"),
            ],
            [
                InlineKeyboardButton("2 Msgs", callback_data="admin_linkguard_msgs:2"),
                InlineKeyboardButton("5 Msgs", callback_data="admin_linkguard_msgs:5"),
                InlineKeyboardButton("10 Msgs", callback_data="admin_linkguard_msgs:10"),
            ],
            [
                InlineKeyboardButton("📋 Review Queue", callback_data="admin_linkguard_review"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="admin_back"),
            ],
        ])

        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data in ["admin_linkguard_on", "admin_linkguard_off"]:
        db.setdefault("link_guard_settings",
        "group_start_hits", {})
        settings = db["link_guard_settings"].setdefault(str(chat.id), {})
        settings["enabled"] = data == "admin_linkguard_on"
        settings.setdefault("max_messages", 2)
        db["link_guard_settings"][str(chat.id)] = settings
        save_db(db)

        query.data = "admin_linkguard"
        await admin_callback(update, context)
        return

    if data.startswith("admin_linkguard_msgs:"):
        n = int(data.split(":", 1)[1])

        db.setdefault("link_guard_settings",
        "group_start_hits", {})
        settings = db["link_guard_settings"].setdefault(str(chat.id), {})
        settings["enabled"] = settings.get("enabled", True)
        settings["max_messages"] = n
        db["link_guard_settings"][str(chat.id)] = settings
        save_db(db)

        query.data = "admin_linkguard"
        await admin_callback(update, context)
        return

    if data == "admin_linkguard_review":
        reviews = [
            (key, item)
            for key, item in db.get("link_review", {}).items()
            if int(item.get("chat_id", 0)) == chat.id
        ]

        if not reviews:
            await query.edit_message_text(
                "📋 Link Guard Review Queue\n\nNo link reviews waiting.",
                reply_markup=back,
            )
            return

        text = f"📋 Link Guard Review Queue: {len(reviews)}\n\n"

        for key, item in reviews[:20]:
            username = item.get("username")
            name = item.get("name") or f"User {item.get('user_id')}"
            label = f"@{username}" if username else name
            msg_text = item.get("text") or ""
            if len(msg_text) > 80:
                msg_text = msg_text[:80] + "..."

            text += (
                f"• {label} — {item.get('user_id')}\n"
                f"  Msg #{item.get('message_count')}: {msg_text}\n\n"
            )

        if len(reviews) > 20:
            text += f"...and {len(reviews) - 20} more."

        await query.edit_message_text(text, reply_markup=back)
        return

    if data == "admin_serverid":
        await query.edit_message_text(
            f"🆔 Server Info\n\nTitle: {chat.title}\nID: {chat.id}",
            reply_markup=back,
        )
        return

    if data == "admin_invitesettings":
        db.setdefault("public_invites",
        "link_guard_settings",
        "group_start_hits", {})
        info = db["public_invites"].get(str(chat.id), {})
        enabled = bool(info.get("enabled"))
        invite_link = info.get("invite_link")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Turn ON", callback_data="admin_invite_on"),
                InlineKeyboardButton("❌ Turn OFF", callback_data="admin_invite_off"),
            ],
            [InlineKeyboardButton("🔁 Create/Refresh Invite", callback_data="admin_invite_refresh")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")],
        ])

        text = (
            "🔗 Invite Settings\n\n"
            f"Server: {chat.title}\n"
            f"ID: {chat.id}\n"
            f"Invite handout: {'ON ✅' if enabled else 'OFF ❌'}\n"
            f"Invite link: {invite_link or 'not set'}"
        )

        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data in ["admin_invite_on", "admin_invite_off"]:
        db.setdefault("public_invites",
        "link_guard_settings",
        "group_start_hits", {})
        info = db["public_invites"].setdefault(str(chat.id), {})
        info["enabled"] = data == "admin_invite_on"
        info["title"] = chat.title or str(chat.id)
        info["server_id"] = chat.id
        db["public_invites"][str(chat.id)] = info
        save_db(db)

        await query.answer("Invite setting updated.", show_alert=True)
        query.data = "admin_invitesettings"
        await admin_callback(update, context)
        return

    if data == "admin_invite_refresh":
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat.id,
                name="Public bot handout invite",
                creates_join_request=False,
            )
        except Exception as e:
            await query.answer(f"Failed to create invite: {e}", show_alert=True)
            return

        db.setdefault("public_invites",
        "link_guard_settings",
        "group_start_hits", {})
        info = db["public_invites"].setdefault(str(chat.id), {})
        info["enabled"] = True
        info["title"] = chat.title or str(chat.id)
        info["server_id"] = chat.id
        info["invite_link"] = invite.invite_link
        info["updated_at"] = int(time.time())
        db["public_invites"][str(chat.id)] = info
        save_db(db)

        await query.answer("Invite created and handout turned ON.", show_alert=True)
        query.data = "admin_invitesettings"
        await admin_callback(update, context)
        return

    if data == "admin_back":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
                InlineKeyboardButton("⏳ Pending", callback_data="admin_pending"),
            ],
            [
                InlineKeyboardButton("🔒 Verify Button", callback_data="admin_verifybutton"),
                InlineKeyboardButton("📣 Reminder Settings", callback_data="admin_remind"),
            ],
            [
                InlineKeyboardButton("⚙️ Reminder Status", callback_data="admin_reminderstatus"),
                InlineKeyboardButton("🆔 Server ID", callback_data="admin_serverid"),
            ],
            [
                InlineKeyboardButton("🔨 Ban User", callback_data="admin_banhelp"),
                
                InlineKeyboardButton("🔗 Invite Settings", callback_data="admin_invitesettings"),
            ],
            [
                InlineKeyboardButton("📜 Rules Settings", callback_data="admin_rulesettings"),
                InlineKeyboardButton("🔗 Link Guard", callback_data="admin_linkguard"),
            ],
        ])

        await query.edit_message_text(
            f"🛠 Admin Menu\n\nActive server:\n{chat.title}\n{chat.id}\n\nChoose an option below:",
            reply_markup=keyboard,
        )
        return

    await query.answer("Unknown admin button.", show_alert=True)


async def link_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    action, chat_id, user_id = parts
    chat_id = int(chat_id)
    user_id = int(user_id)

    # Only admins can use review buttons
    try:
        reviewer = await context.bot.get_chat_member(chat_id, query.from_user.id)
        if reviewer.status not in ["administrator", "creator"]:
            await query.answer("Admins only.", show_alert=True)
            return
    except Exception:
        await query.answer("Could not verify admin status.", show_alert=True)
        return

    if action == "linkapprove":
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=UNLOCKED_PERMS,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to approve {user_id}: {e}")
            return

        db = load_db()
        db.get("link_review", {}).pop(f"{chat_id}:{user_id}", None)
        save_db(db)

        await query.edit_message_text(f"✅ User {user_id} approved and unmuted.")

    elif action == "linkban":
        try:
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to ban {user_id}: {e}")
            return

        db = load_db()
        db.get("link_review", {}).pop(f"{chat_id}:{user_id}", None)
        save_db(db)

        await query.edit_message_text(f"🔨 User {user_id} banned.")


def format_scan_time(timestamp):
    if not timestamp:
        return "never"

    try:
        return datetime.fromtimestamp(
            int(timestamp),
            ZoneInfo("America/New_York"),
        ).strftime("%Y-%m-%d %I:%M %p ET")
    except Exception:
        return str(timestamp)


async def get_readonly_stats(bot, db, chat_id):
    """Calculate stats without modifying invite_db.json or its audit section."""
    chat_key = str(chat_id)
    verified_ids = set(db.get("verified", {}).get(chat_key, {}))
    pending_ids = {
        str(uid)
        for uid, info in db.get("pending_verify", {}).items()
        if str(info.get("chat_id")) == chat_key
    }

    try:
        telegram_total = await bot.get_chat_member_count(chat_id)
    except Exception:
        telegram_total = None

    try:
        administrators = await bot.get_chat_administrators(chat_id)
        admin_count = len(administrators)
    except Exception:
        admin_count = None

    overlap = verified_ids & pending_ids
    classified_ids = verified_ids | pending_ids
    unknown_estimate = None
    if telegram_total is not None:
        unknown_estimate = max(telegram_total - len(classified_ids), 0)

    return {
        "telegram_total": telegram_total,
        "verified": len(verified_ids),
        "pending": len(pending_ids),
        "overlap": len(overlap),
        "classified": len(classified_ids),
        "unknown_estimate": unknown_estimate,
        "admins": admin_count,
    }


def format_readonly_stats(chat_title, stats, links, bans, reviews):
    total = (
        stats["telegram_total"]
        if stats["telegram_total"] is not None
        else "unavailable"
    )
    admins = stats["admins"] if stats["admins"] is not None else "unavailable"
    unknown = (
        stats["unknown_estimate"]
        if stats["unknown_estimate"] is not None
        else "unavailable"
    )

    return (
        f"📊 Stats for {chat_title}\n\n"
        f"👥 Telegram member total: {total}\n"
        f"✅ Verified database records: {stats['verified']}\n"
        f"⏳ Pending database records: {stats['pending']}\n"
        f"❔ Unclassified estimate: {unknown}\n"
        f"🛡 Current admins: {admins}\n"
        f"⚠️ Verified/pending overlap: {stats['overlap']}\n\n"
        f"🔗 Personal invite links: {links}\n"
        f"🔨 Persistent bans: {bans}\n"
        f"🚨 Link reviews awaiting admin: {reviews}\n\n"
        "Read-only view: no database records were changed.\n"
        "Database counts may include former members; Telegram total is live."
    )


def build_audit_member(member, verified_ids, pending_ids):
    uid = str(member.user.id)
    status = str(member.status).lower()
    is_admin = status in ["administrator", "creator"]
    is_restricted = status == "restricted"

    if uid in verified_ids and not is_restricted:
        verify_status = "verified"
    elif uid in pending_ids or is_restricted:
        verify_status = "pending"
    elif is_admin or member.user.is_bot:
        verify_status = "verified"
    else:
        verify_status = "unknown"

    return {
        "user_id": member.user.id,
        "username": member.user.username,
        "name": member.user.full_name,
        "telegram_status": status,
        "is_bot": bool(member.user.is_bot),
        "is_deleted": False,
        "is_admin": is_admin,
        "verify_status": verify_status,
        "in_verified_db": uid in verified_ids,
        "in_pending_db": uid in pending_ids,
        "updated_at": int(time.time()),
    }


async def refreshstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Run /refreshstats inside the server.")
        return

    if not await user_is_admin(update, context):
        return

    chat = update.effective_chat

    try:
        await update.message.delete()
    except Exception:
        pass

    db = load_db()
    stats = await get_readonly_stats(context.bot, db, chat.id)
    links_here = sum(
        1
        for info in db.get("links", {}).values()
        if str(info.get("chat_id")) == str(chat.id)
    )
    bans_here = len(db.get("bans", {}).get(str(chat.id), {}))
    reviews_here = sum(
        1
        for info in db.get("link_review", {}).values()
        if int(info.get("chat_id", 0)) == chat.id
    )

    text = format_readonly_stats(
        chat.title,
        stats,
        links_here,
        bans_here,
        reviews_here,
    )

    try:
        await update.effective_user.send_message(text)
    except Exception:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"{update.effective_user.mention_html()} start a DM with me first, "
                "then run /refreshstats again."
            ),
            parse_mode="HTML",
        )


async def refreshstats_worker(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_key: str,
):
    # Legacy reconciliation is intentionally disabled. Stats are read-only.
    return

    chat = update.effective_chat
    progress = await update.message.reply_text(
        "🔄 Refreshing server stats from Telegram..."
    )

    try:
        db = load_db()
        users = db.get("users", {})
        verified = db.get("verified", {}).get(chat_key, {})
        pending_all = db.get("pending_verify", {})
        verified_ids = set(verified)
        pending_ids = {
            str(uid)
            for uid, info in pending_all.items()
            if str(info.get("chat_id")) == chat_key
        }
        old_audit = db.get("member_audit", {}).get(chat_key, {})
        old_members = old_audit.get("members", {})

        try:
            telegram_member_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            telegram_member_count = old_audit.get("telegram_member_count")

        known_ids = {
            str(uid)
            for uid, info in users.items()
            if chat_key in info.get("servers", {})
        }
        known_ids.update(str(uid) for uid in verified)
        known_ids.update(pending_ids)
        known_ids.update(str(uid) for uid in old_members)

        try:
            administrators = await context.bot.get_chat_administrators(chat.id)
            known_ids.update(str(member.user.id) for member in administrators)
        except Exception:
            pass

        new_members = {}
        departed_ids = set()
        error_ids = set()
        checked = 0
        total = len(known_ids)

        ordered_ids = sorted(
            known_ids,
            key=lambda value: (
                0,
                int(value),
            ) if str(value).lstrip("-").isdigit() else (1, str(value)),
        )

        for processed, uid in enumerate(ordered_ids, start=1):
            member = None

            if not str(uid).lstrip("-").isdigit():
                error_ids.add(uid)
                continue

            try:
                member = await context.bot.get_chat_member(chat.id, int(uid))
                checked += 1
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after:
                    await asyncio.sleep(float(retry_after) + 1)
                    try:
                        member = await context.bot.get_chat_member(chat.id, int(uid))
                        checked += 1
                    except Exception:
                        member = None

            if member is None:
                error_ids.add(uid)
                if uid in old_members:
                    new_members[uid] = {
                        **old_members[uid],
                        "scan_error": True,
                        "updated_at": int(time.time()),
                    }
            else:
                status = str(member.status).lower()
                if status in ["left", "kicked", "banned"]:
                    departed_ids.add(uid)
                elif status in ["member", "administrator", "creator", "restricted"]:
                    new_members[uid] = build_audit_member(
                        member,
                        verified_ids,
                        pending_ids,
                    )
                else:
                    error_ids.add(uid)

            if processed % 50 == 0:
                try:
                    await progress.edit_text(
                        f"🔄 Refreshing server stats... {processed}/{total} processed"
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.04)

        # Reload before saving so messages handled during the background scan
        # are not overwritten by the snapshot captured at scan start.
        latest_db = load_db()
        latest_pending = latest_db.get("pending_verify", {})
        latest_verified = latest_db.get("verified", {}).get(chat_key, {})
        pending_removed = 0

        for uid in list(latest_pending):
            info = latest_pending.get(uid, {})
            if str(info.get("chat_id")) != chat_key:
                continue
            if uid in latest_verified or uid in departed_ids:
                latest_pending.pop(uid, None)
                pending_removed += 1

        now = int(time.time())
        latest_db.setdefault("member_audit", {})
        latest_db["member_audit"][chat_key] = {
            "chat_id": chat.id,
            "title": chat.title or chat_key,
            "scanned_at": now,
            "checked_count": checked,
            "known_count": total,
            "telegram_member_count": telegram_member_count,
            "departed_count": len(departed_ids),
            "error_count": len(error_ids),
            "members": new_members,
        }
        latest_db["pending_verify"] = latest_pending
        save_db(latest_db)

        stats = get_audit_stats(latest_db, chat.id)
        total_line = (
            f"👥 Telegram member total: {stats['telegram_total']}\n"
            if stats["telegram_total"] is not None
            else ""
        )
        await progress.edit_text(
            f"✅ Stats refreshed for {chat.title}\n\n"
            f"{total_line}"
            f"🗂 Active tracked members: {stats['active']}\n"
            f"✅ Verified: {stats['verified']}\n"
            f"⏳ Pending: {stats['pending']}\n"
            f"❔ Unknown: {stats['unknown']}\n"
            f"🛡 Admins: {stats['admins']}\n"
            f"🤖 Bots: {stats['bots']}\n"
            f"🚪 Left or banned: {stats['departed']}\n"
            f"⚠️ Lookup errors: {stats['errors']}\n"
            f"🧹 Stale pending records removed: {pending_removed}\n\n"
            "Use /stats to view this snapshot."
        )
    except Exception as exc:
        print(f"[REFRESH STATS ERROR] chat={chat_key} error={exc}", flush=True)
        try:
            await progress.edit_text(
                "❌ Stats refresh failed. Check the service logs for details."
            )
        except Exception:
            pass
    finally:
        context.application.bot_data.get(
            "running_stats_scans",
            set(),
        ).discard(chat_key)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    chat = update.effective_chat
    db = load_db()
    bans = db.get("bans", {}).get(str(chat.id), {})
    links = db.get("links", {})
    link_review = db.get("link_review", {})
    stats = await get_readonly_stats(context.bot, db, chat.id)

    links_here = [
        info for info in links.values()
        if str(info.get("chat_id")) == str(chat.id)
    ]

    review_here = [
        info for info in link_review.values()
        if int(info.get("chat_id", 0)) == chat.id
    ]

    text = format_readonly_stats(
        chat.title,
        stats,
        len(links_here),
        len(bans),
        len(review_here),
    )

    try:
        await update.effective_user.send_message(text)
    except Exception:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"{update.effective_user.mention_html()} start a DM with me first, then run /stats again.",
            parse_mode="HTML",
        )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    db = load_db()
    users = list(db.get("users", {}).values())

    users_here = [
        u for u in users
        if str(update.effective_chat.id) in u.get("servers", {})
    ]

    if not users_here:
        text = f"No stored users for {update.effective_chat.title}."
    else:
        text = f"Stored users for {update.effective_chat.title}: {len(users_here)}\n\n"
        for u in users_here[:100]:
            username = u.get("username")
            name = u.get("name") or "Unknown"
            label = f"@{username}" if username else name
            text += f"• {label} — {u.get('user_id')}\n"

    try:
        await update.effective_user.send_message(text)
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{update.effective_user.mention_html()} start a DM with me first, then run /users again.",
            parse_mode="HTML",
        )

async def serverid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat

    await update.message.reply_text(
        f"Server title: {chat.title}\n"
        f"Server ID: {chat.id}"
    )


async def mystoredservers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    db = load_db()
    user = update.effective_user
    info = db.get("users", {}).get(str(user.id), {})
    servers = info.get("servers", {})

    if not servers:
        await update.message.reply_text("No servers stored for you yet.")
        return

    text = "Stored servers for you:\n\n"
    for sid, sinfo in servers.items():
        text += f"• {sinfo.get('chat_title', 'unknown')} — {sid}\n"

    await update.message.reply_text(text)


# -------------------------
# Admin moderation/recovery commands
# -------------------------

async def nukeinvite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/nukeinvite ORIGINAL_SERVER_ID optional message"
        )
        return

    try:
        source_server_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("First value must be the original server ID.")
        return

    custom_msg = " ".join(context.args[1:]).strip() or "Backup/recovery invite. Use this link to rejoin:"
    destination_chat = update.effective_chat

    invite = await context.bot.create_chat_invite_link(
        chat_id=destination_chat.id,
        name=f"nuke_recovery_{source_server_id}_{int(time.time())}",
        creates_join_request=False,
    )

    db = load_db()
    users = db.get("users", {})
    source_key = str(source_server_id)
    matched = []

    for uid, info in users.items():
        if info.get("is_bot"):
            continue
        if source_key in info.get("servers", {}):
            matched.append(uid)

    sent = 0
    failed = 0
    await update.message.reply_text(f"Starting recovery invite to {len(matched)} stored user(s).")

    for uid in matched:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"{custom_msg}\n\n{invite.invite_link}",
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Recovery invite complete. Sent: {sent} Failed: {failed}")


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    user_id = None
    if update.message.reply_to_message:
        user_id = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            user_id = int(context.args[0])
        except Exception:
            pass

    if not user_id:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /approve USER_ID or reply to a user with /approve",
        )
        return

    pending = get_pending_verify(user_id)
    chat_id = int(pending.get("chat_id")) if pending else update.effective_chat.id

    try:
        chat_obj = await context.bot.get_chat(chat_id)
        chat_title = chat_obj.title
    except Exception:
        chat_title = str(chat_id)

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=UNLOCKED_PERMS,
        )
        await set_member_tag(chat_id, user_id, None)
        complete_verification(
            user_id,
            chat_id,
            chat_title,
            source="manual_approve",
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ User {user_id} approved in {chat_title}.",
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Failed to approve {user_id}: {e}",
        )


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    target = None
    reason = ""

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        reason = " ".join(context.args).strip()
    elif context.args:
        try:
            user_id = int(context.args[0])
            class TempUser:
                pass
            target = TempUser()
            target.id = user_id
            target.username = None
            target.full_name = f"User {user_id}"
            reason = " ".join(context.args[1:]).strip()
        except Exception:
            target = None

    if not target:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /ban USER_ID reason or reply to a user with /ban reason",
        )
        return

    add_persist_ban(update.effective_chat.id, target, update.effective_user, reason)

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔨 Persistent ban saved for {target.full_name}. Reason: {reason or 'none'}",
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Saved persistent ban, but Telegram ban failed: {e}",
        )


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    if not context.args:
        await context.bot.send_message(update.effective_chat.id, "Usage: /unban USER_ID")
        return

    try:
        user_id = int(context.args[0])
    except Exception:
        await context.bot.send_message(update.effective_chat.id, "USER_ID must be a number.")
        return

    remove_persist_ban(update.effective_chat.id, user_id)

    try:
        await context.bot.unban_chat_member(update.effective_chat.id, user_id, only_if_banned=True)
    except Exception:
        pass

    await context.bot.send_message(update.effective_chat.id, f"✅ Persistent ban removed for {user_id}.")


async def banlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    db = load_db()
    bans = db.get("bans", {}).get(str(update.effective_chat.id), {})

    if not bans:
        dm_text = f"No persistent bans for {update.effective_chat.title}."
    else:
        dm_text = f"Persistent bans for {update.effective_chat.title}:\n\n"
        for uid, info in list(bans.items())[:100]:
            username = info.get("username")
            name = info.get("name", "Unknown")
            reason = info.get("reason") or "none"
            label = f"@{username}" if username else name
            dm_text += f"• {label} — {uid}\n  Reason: {reason}\n"

    try:
        await update.effective_user.send_message(dm_text)
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{update.effective_user.mention_html()} start a DM with me first, then run /banlist again.",
            parse_mode="HTML",
        )

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

    if update.effective_chat.type not in ["group", "supergroup"]:
        return

    if not await user_is_admin(update, context):
        return

    chat = update.effective_chat
    db = load_db()
    pending = {
        uid: info for uid, info in db.get("pending_verify", {}).items()
        if int(info.get("chat_id", 0)) == chat.id
    }

    if not pending:
        text = f"No users are pending verification for {chat.title}."
    else:
        text = f"Pending verification for {chat.title}: {len(pending)}\n\n"
        for uid, info in list(pending.items())[:150]:
            text += (
                f"• {info.get('name', 'Unknown')} (@{info.get('username') or 'no_username'})\n"
                f"  ID: {uid}\n"
            )

    try:
        await update.effective_user.send_message(text)
    except Exception:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"{update.effective_user.mention_html()} start a DM with me first, then run /pending again.",
            parse_mode="HTML",
        )

def get_pending_for_server(db, server_id):
    return [
        (uid, info)
        for uid, info in db.get("pending_verify", {}).items()
        if int(info.get("chat_id", 0)) == int(server_id)
    ]


async def post_pending_reminders(context, server_id):
    db = load_db()
    settings = db.get("verify_reminders", {}).get(str(server_id))

    if not settings:
        return

    verified_ids = set(db.get("verified", {}).get(str(server_id), {}).keys())

    pending = [
        (uid, info)
        for uid, info in get_pending_for_server(db, server_id)
        if str(uid) not in verified_ids
    ]

    # Clean verified users out of pending_verify so they stop being reminded.
    changed = False
    for uid in verified_ids:
        if uid in db.get("pending_verify", {}):
            db["pending_verify"].pop(uid, None)
            changed = True

    if changed:
        save_db(db)

    if not pending:
        return

    batch_size = int(settings.get("batch_size", 10))
    index = int(settings.get("index", 0))

    batch = pending[index:index + batch_size]
    if not batch:
        index = 0
        batch = pending[:batch_size]

    if not batch:
        return

    bot_info = await context.bot.get_me()
    verify_url = f"https://t.me/{bot_info.username}?start=verify"

    lines = []
    for uid, info in batch:
        name = info.get("name") or f"User {uid}"
        username = info.get("username")
        if username:
            lines.append(f"• @{username}")
        else:
            lines.append(f'• <a href="tg://user?id={uid}">{name}</a>')

    text = (
        "🔒 Verification Reminder\n\n"
        "The following users still need to complete verification:\n\n"
        + "\n".join(lines)
        + "\n\nClick below, send /start, then acknowledge the rules to unlock chat."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Verify Access", url=verify_url)]
    ])

    kwargs = {
        "chat_id": int(settings["reminder_chat_id"]),
        "text": text,
        "reply_markup": keyboard,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if settings.get("message_thread_id"):
        kwargs["message_thread_id"] = int(settings["message_thread_id"])

    await context.bot.send_message(**kwargs)

    settings["index"] = (index + batch_size) % max(len(pending), 1)
    settings["last_run"] = int(time.time())
    db["verify_reminders"][str(server_id)] = settings
    save_db(db)


async def reminder_background_loop(app):
    while True:
        try:
            db = load_db()
            settings_all = db.get("verify_reminders", {})

            for server_id, settings in list(settings_all.items()):
                interval = int(settings.get("interval_minutes", 60)) * 60
                last_run = int(settings.get("last_run", 0))

                hour = datetime.now(ZoneInfo("America/New_York")).hour
                quiet_start = int(settings.get("quiet_start_hour", 21))
                quiet_end = int(settings.get("quiet_end_hour", 8))

                in_quiet_hours = hour >= quiet_start or hour < quiet_end

                if in_quiet_hours:
                    continue

                if int(time.time()) - last_run >= interval:
                    class Ctx:
                        pass
                    ctx = Ctx()
                    ctx.bot = app.bot
                    await post_pending_reminders(ctx, int(server_id))

        except Exception as e:
            print(f"[REMINDER LOOP ERROR] {e}", flush=True)

        await asyncio.sleep(60)


async def start_reminder_loop(app):
    asyncio.create_task(reminder_background_loop(app))


async def setreminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Convenience command: run /setreminder inside the group/topic where reminders should post.
    if not update.message:
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Use /setreminder inside the group/topic where reminder posts should go.")
        return

    if not await user_is_admin(update, context):
        return

    server_id = update.effective_chat.id

    db = load_db()
    db.setdefault("verify_reminders", {})

    existing = db["verify_reminders"].get(str(server_id), {})

    db["verify_reminders"][str(server_id)] = {
        "server_id": server_id,
        "reminder_chat_id": update.effective_chat.id,
        "message_thread_id": update.message.message_thread_id,
        "interval_minutes": int(existing.get("interval_minutes", 60)),
        "batch_size": int(existing.get("batch_size", 10)),
        "index": int(existing.get("index", 0)),
        "last_run": int(existing.get("last_run", 0)),
    }

    save_db(db)

    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        await update.effective_user.send_message(
            "✅ Reminder channel/topic saved.\n\n"
            f"Server: {server_id}\n"
            f"Reminder chat: {update.effective_chat.id}\n"
            f"Topic ID: {update.message.message_thread_id}\n\n"
            "Open /admin → Reminder Settings to adjust interval and batch size."
        )
    except Exception:
        pass


async def setreminderchannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/setreminderchannel SERVER_ID\n\n"
            "Example:\n/setreminderchannel -1003907893676"
        )
        return

    try:
        server_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("SERVER_ID must be a number.")
        return

    try:
        member = await context.bot.get_chat_member(server_id, update.effective_user.id)
        if member.status not in ["administrator", "creator"]:
            await update.message.reply_text("Only admins of that server can set reminders.")
            return
    except Exception as e:
        await update.message.reply_text(f"Could not verify admin status for that server: {e}")
        return

    db = load_db()
    db.setdefault("verify_reminders", {})

    existing = db["verify_reminders"].get(str(server_id), {})

    db["verify_reminders"][str(server_id)] = {
        "server_id": server_id,
        "reminder_chat_id": update.effective_chat.id,
        "message_thread_id": update.message.message_thread_id,
        "interval_minutes": int(existing.get("interval_minutes", 60)),
        "batch_size": int(existing.get("batch_size", 10)),
        "index": int(existing.get("index", 0)),
        "last_run": int(existing.get("last_run", 0)),
    }

    save_db(db)

    await update.message.reply_text(
        "✅ Verification reminder channel/topic saved.\n\n"
        f"Server: {server_id}\n"
        f"Reminder chat: {update.effective_chat.id}\n"
        f"Topic ID: {update.message.message_thread_id}"
    )


async def setreminderinterval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n/setreminderinterval SERVER_ID MINUTES\n\n"
            "Example:\n/setreminderinterval -1003907893676 60"
        )
        return

    server_id = str(int(context.args[0]))
    minutes = int(context.args[1])

    db = load_db()
    settings = db.setdefault("verify_reminders", {}).setdefault(server_id, {})
    settings["interval_minutes"] = minutes
    db["verify_reminders"][server_id] = settings
    save_db(db)

    await update.message.reply_text(f"✅ Reminder interval set to {minutes} minutes.")


async def setreminderbatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n/setreminderbatch SERVER_ID NUMBER\n\n"
            "Example:\n/setreminderbatch -1003907893676 10"
        )
        return

    server_id = str(int(context.args[0]))
    batch_size = int(context.args[1])

    db = load_db()
    settings = db.setdefault("verify_reminders", {}).setdefault(server_id, {})
    settings["batch_size"] = batch_size
    db["verify_reminders"][server_id] = settings
    save_db(db)

    await update.message.reply_text(f"✅ Reminder batch size set to {batch_size}.")


async def reminderstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    settings_all = db.get("verify_reminders", {})

    if not settings_all:
        await update.message.reply_text("No verification reminders configured.")
        return

    text = "Verification reminder settings:\n\n"

    for server_id, settings in settings_all.items():
        pending = len(get_pending_for_server(db, int(server_id)))
        text += (
            f"Server: {server_id}\n"
            f"Reminder chat: {settings.get('reminder_chat_id')}\n"
            f"Topic ID: {settings.get('message_thread_id')}\n"
            f"Interval: {settings.get('interval_minutes', 60)} minutes\n"
            f"Batch size: {settings.get('batch_size', 10)}\n"
            f"Pending: {pending}\n\n"
        )

    await update.message.reply_text(text)


async def remindpending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/remindpending SERVER_ID\n\n"
            "Example:\n/remindpending -1003907893676"
        )
        return

    server_id = int(context.args[0])
    await post_pending_reminders(context, server_id)
    await update.message.reply_text("✅ Reminder batch sent.")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).post_init(start_reminder_loop).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))

    app.add_handler(CommandHandler("myinvite", myinvite_cmd))
    app.add_handler(CommandHandler("myinvites", myinvites_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("serverid", serverid_cmd))
    app.add_handler(CommandHandler("mystoredservers", mystoredservers_cmd))

    app.add_handler(CommandHandler("nukeinvite", nukeinvite_cmd))

    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("banlist", banlist_cmd))
    app.add_handler(CommandHandler("repairpending", repairpending_cmd))
    app.add_handler(CommandHandler("refreshstats", refreshstats_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("verificationbutton", verificationbutton_cmd))
    app.add_handler(CommandHandler("setreminder", setreminder_cmd))
    app.add_handler(CommandHandler("setreminderchannel", setreminderchannel_cmd))
    app.add_handler(CommandHandler("setreminderinterval", setreminderinterval_cmd))
    app.add_handler(CommandHandler("setreminderbatch", setreminderbatch_cmd))
    app.add_handler(CommandHandler("reminderstatus", reminderstatus_cmd))
    app.add_handler(CommandHandler("remindpending", remindpending_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL, early_link_guard))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    app.add_handler(CallbackQueryHandler(link_review_callback, pattern=r"^link(approve|ban):"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(agree_callback, pattern=r"^agree:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_touch_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
