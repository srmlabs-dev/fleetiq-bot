"""
FleetIQ Support Bot v1.1.1
SRM Labs

Telegram support/community bot for FleetIQ Driver.

Production safety patch over v1.1:
- Isolates private/group sessions by user_id + chat_id
- Group commands require inline text and do not leak private state
- Group callbacks redirect users to private chat
- Adds topic/chat context for FleetIQ Brain
- Adds secret redaction for logs
- Keeps async httpx integration
"""

import os
import json
import logging
import asyncio
from datetime import datetime, time
from urllib.parse import quote
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SHEETS_URL     = os.environ.get("SHEETS_URL", "")
SHEETS_SECRET  = os.environ.get("COMMUNITY_API_SECRET", "")
OWNER_ID       = int(os.environ.get("OWNER_ID", "7563117271"))
APP_VERSION    = os.environ.get("APP_VERSION", "1.1.1")

BOT_USERNAME   = os.environ.get("BOT_USERNAME", "FleetIQSupport_bot")
COMMUNITY_URL  = os.environ.get("COMMUNITY_URL", "https://t.me/+ktZOiC7_bMowZmEx")
APP_URL        = os.environ.get("APP_URL", "https://srmlabs-dev.github.io/fleetiq-driver")

SUPPORTED_LANGS = {
    "en": "🇺🇸 English",
    "ru": "🇷🇺 Русский",
    "es": "🇪🇸 Español",
    "tr": "🇹🇷 Türkçe",
    "uk": "🇺🇦 Українська",
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("FleetIQBot")


# ── SYSTEM PROMPT FOR CLAUDE ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are FleetIQ Support Bot — a friendly, knowledgeable assistant for FleetIQ Driver app by SRM Labs.

FleetIQ Driver is a PWA (Progressive Web App) for truck drivers and owner-operators. It helps with:
- Load tracking (add, edit, delete loads with miles, gross, driver pay)
- PTI (Pre-Trip Inspection) — daily and weekly checklists
- Settlement reports — PDF and CSV export
- Google Sheets sync for fleet managers
- Dispute tracking for cancelled/adjusted loads
- Pay calculation: CPM (cents per mile) or % of gross
- Community and support connection through Telegram

Key facts:
- App URL: srmlabs-dev.github.io/fleetiq-driver
- Works offline (PWA with Service Worker)
- Can be installed on phone from Chrome browser
- Data syncs to Google Sheets via Apps Script URL
- Free to use, no subscription
- FleetIQ Community is the Telegram support/community group
- Users can report bugs, share ideas, ask questions, invite drivers, and optionally share anonymized data later

Your job:
1. Answer user questions about FleetIQ clearly and helpfully
2. Help troubleshoot issues step by step
3. Collect feedback, bugs, and ideas
4. Be concise — users are truck drivers on mobile
5. In group chats, stay short and do not flood the group

Always respond in the same language the user writes in.
If user writes in Russian, respond in Russian.
If user writes in English, respond in English.

For bugs, always ask:
- What device/OS?
- What exactly happened vs what was expected?
- Which part of the app? (loads/pti/sync/reports/settings)

Keep responses short and practical. No fluff."""


# ── HELPERS ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def redact_secrets(text: str) -> str:
    """Redact known secrets before writing logs or user-visible errors."""
    if text is None:
        return ""
    safe = str(text)
    for secret in (BOT_TOKEN, ANTHROPIC_KEY, SHEETS_SECRET):
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def user_payload(user, extra: Optional[dict] = None) -> dict:
    payload = {
        "telegram_id": str(user.id),
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "language": getattr(user, "language_code", None) or "",
    }
    if extra:
        payload.update(extra)
    return payload


def to_base36(num: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return "0"
    out = ""
    n = abs(int(num))
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out


def from_base36(value: str) -> Optional[int]:
    try:
        return int(value.lower().strip(), 36)
    except Exception:
        return None


def make_ref_code(user_id: int) -> str:
    # MVP referral code. Later replace with backend-generated random code.
    return "u" + to_base36(user_id)


def parse_ref_code(code: str) -> Optional[int]:
    if not code:
        return None
    code = code.strip()
    if code.startswith("u"):
        return from_base36(code[1:])
    return from_base36(code)


def get_chat_context(update: Update) -> dict:
    chat = update.effective_chat
    msg = update.effective_message

    return {
        "chat_id": str(chat.id) if chat else "",
        "chat_title": getattr(chat, "title", "") or "",
        "chat_type": getattr(chat, "type", "") or "",
        "message_id": str(msg.message_id) if msg else "",
        "topic_id": str(getattr(msg, "message_thread_id", "") or "") if msg else "",
        "source": "telegram_group" if chat and chat.type in ("group", "supergroup") else "telegram_private",
    }


def command_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(ctx.args).strip() if ctx.args else ""


# ── GOOGLE SHEETS / APPS SCRIPT BACKEND ───────────────────────────────────────

async def post_to_sheets(payload: dict) -> dict:
    """POST to Apps Script backend. Returns JSON response if possible."""
    if not SHEETS_URL:
        log.warning("SHEETS_URL not configured")
        return {"ok": False, "error": "SHEETS_URL not configured"}

    full_payload = dict(payload)
    if SHEETS_SECRET:
        full_payload["secret"] = SHEETS_SECRET

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                SHEETS_URL,
                headers={"Content-Type": "text/plain"},
                content=json.dumps(full_payload, ensure_ascii=False),
            )
        try:
            data = resp.json()
        except Exception:
            data = {"ok": resp.status_code < 400, "raw": resp.text[:500]}

        if not data.get("ok"):
            log.warning("Sheets returned not ok: %s", redact_secrets(json.dumps(data, ensure_ascii=False)[:800]))
        return data

    except Exception as e:
        log.error("Sheets post error: %s", redact_secrets(str(e)))
        return {"ok": False, "error": "sheets_post_error"}


async def save_user_seen(update: Update, source: str = "", entry_point: str = "", extra: Optional[dict] = None):
    user = update.effective_user
    if not user:
        return

    payload = user_payload(user, {
        "action": "user_seen",
        "source": source or get_chat_context(update).get("source", ""),
        "entry_point": entry_point,
        **get_chat_context(update),
    })
    if extra:
        payload.update(extra)

    asyncio.create_task(post_to_sheets(payload))


async def save_structured_feedback(
    update: Update,
    text: str,
    msg_type: str,
    classification: dict,
    response: str = "",
    entry_point: str = "",
) -> dict:
    """Save bug/idea/question/feedback using backend actions with fallback."""
    user = update.effective_user
    ctx_payload = get_chat_context(update)

    base = user_payload(user, {
        "timestamp": now_iso(),
        "msg_type": msg_type,
        "message": text,
        "module": classification.get("module", "other"),
        "priority": classification.get("priority", "medium"),
        "summary": classification.get("summary", ""),
        "ai_prompt": classification.get("engineering_prompt", ""),
        "engineering_prompt": classification.get("engineering_prompt", ""),
        "bot_response": response[:500],
        "app_version": APP_VERSION,
        "entry_point": entry_point,
        **ctx_payload,
    })

    if msg_type == "bug":
        primary = {
            **base,
            "action": "bug_report",
            "bug_description": text,
            "severity": "high" if classification.get("priority") == "high" else "low",
        }
    elif msg_type == "idea":
        primary = {
            **base,
            "action": "idea_submit",
            "idea_text": text,
        }
    elif msg_type == "question":
        # If Apps Script supports question_submit, it will save to Questions.
        # If not, fallback below saves it as feedback_submit.
        primary = {
            **base,
            "action": "question_submit",
            "question_text": text,
            "answer_text": response[:1000],
        }
    elif msg_type == "rating":
        primary = {
            **base,
            "action": "feedback_submit",
            "topic": "rating",
            "message": text,
        }
    else:
        primary = {
            **base,
            "action": "feedback_submit",
            "topic": msg_type or "general",
            "message": text,
        }

    result = await post_to_sheets(primary)

    # Fallback if backend does not support question_submit or other new action yet.
    if not result.get("ok") and msg_type in ("question", "feedback", "complaint"):
        fallback = {
            **base,
            "action": "feedback_submit",
            "topic": msg_type,
            "message": text,
        }
        result = await post_to_sheets(fallback)

    return result


# ── CLAUDE API ────────────────────────────────────────────────────────────────

async def ask_claude(messages: list, system: str = SYSTEM_PROMPT) -> str:
    """Call Claude API and return response text."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 1000,
                    "system": system,
                    "messages": messages,
                }
            )
            data = resp.json()
            log.info("Claude response keys: %s", list(data.keys()))
            if "content" in data and data["content"]:
                return data["content"][0]["text"]
            if "error" in data:
                log.error("Claude API error response: %s", redact_secrets(str(data["error"])))
            return "Sorry, I couldn't process that. Please try again."
    except Exception as e:
        log.error("Claude API error: %s", redact_secrets(str(e)))
        return "⚠️ AI service temporarily unavailable. Please try again in a moment."


async def classify_feedback(text: str, msg_type: str) -> dict:
    """Ask Claude to classify and create engineering prompt for feedback."""
    prompt = f"""Analyze this user feedback for FleetIQ Driver app.

Type hint from user: {msg_type}
Message: {text}

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "type": "bug|idea|question|complaint|rating|feedback",
  "priority": "high|medium|low",
  "module": "loads|pti|sync|reports|settings|disputes|install|community|referral|data_sharing|other",
  "summary": "one line summary max 80 chars",
  "engineering_prompt": "Ready-to-use prompt for a developer AI. Be specific: what to fix/add, which file/module, what NOT to touch. Max 300 chars."
}}"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                }
            )
            data = resp.json()
            raw = data["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        log.error("Classify error: %s", redact_secrets(str(e)))
        return {
            "type": msg_type,
            "priority": "medium",
            "module": "other",
            "summary": text[:80],
            "engineering_prompt": f"User reported: {text[:200]}"
        }


# ── USER SESSION MVP ─────────────────────────────────────────────────────────

user_sessions: dict = {}  # key: user_id:chat_id → session

def session_key(user_id: int, chat_id: Optional[int]) -> str:
    return f"{user_id}:{chat_id or 'private'}"


def get_session(user_id: int, chat_id: Optional[int] = None) -> dict:
    key = session_key(user_id, chat_id)
    if key not in user_sessions:
        user_sessions[key] = {
            "history": [],
            "mode": None,
            "language": None,
            "entry_point": None,
        }
    return user_sessions[key]


def current_session(update: Update) -> dict:
    user = update.effective_user
    chat = update.effective_chat
    return get_session(user.id, chat.id if chat else None)


# ── MENUS ────────────────────────────────────────────────────────────────────

def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐛 Report Bug", callback_data="mode_bug"),
            InlineKeyboardButton("💡 Share Idea", callback_data="mode_idea"),
        ],
        [
            InlineKeyboardButton("❓ Ask Question", callback_data="mode_question"),
            InlineKeyboardButton("⭐ Rate App", callback_data="mode_rate"),
        ],
        [
            InlineKeyboardButton("🌐 Community", callback_data="community_menu"),
            InlineKeyboardButton("🎁 Invite Driver", callback_data="invite_driver"),
        ],
        [
            InlineKeyboardButton("🤝 Share Anonymous Data", callback_data="data_share_info"),
        ],
        [
            InlineKeyboardButton("📖 How to Use", callback_data="mode_howto"),
            InlineKeyboardButton("🌍 Language", callback_data="language_menu"),
        ],
    ])


def community_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Join FleetIQ Community", url=COMMUNITY_URL)],
        [InlineKeyboardButton("🆘 Open Support", callback_data="mode_question")],
        [
            InlineKeyboardButton("🐛 Report Bug", callback_data="mode_bug"),
            InlineKeyboardButton("💡 Share Idea", callback_data="mode_idea"),
        ],
        [
            InlineKeyboardButton("❓ Ask Question", callback_data="mode_question"),
            InlineKeyboardButton("🎁 Invite Driver", callback_data="invite_driver"),
        ],
    ])


def language_menu_markup() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"lang_{code}")]
            for code, label in SUPPORTED_LANGS.items()]
    return InlineKeyboardMarkup(rows)


def share_invite_markup(invite_link: str, text: str) -> InlineKeyboardMarkup:
    share_url = f"https://t.me/share/url?url={quote(invite_link)}&text={quote(text)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Invite", url=share_url)],
        [InlineKeyboardButton("🌐 Join Community", url=COMMUNITY_URL)],
    ])


def private_link_markup(start_arg: str = "from_group") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Open Private Support", url=f"https://t.me/{BOT_USERNAME}?start={start_arg}")]
    ])


async def send_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: Optional[str] = None):
    user = update.effective_user
    msg = text or (
        f"👋 Hi {user.first_name}!\n\n"
        f"I'm FleetIQ Support Bot — your assistant for the *FleetIQ Driver* app.\n\n"
        f"I can help with support, bugs, ideas, questions, referrals and community access.\n\n"
        f"What can I help you with?"
    )

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_markup())
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_menu_markup())


async def send_community_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌐 *FleetIQ Community & Support*\n\n"
        "Use these options to join the community, get help, report bugs, share ideas or invite drivers.\n\n"
        "Community is where drivers can discuss app support, loads, settlements, maintenance and business topics."
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=community_menu_markup())
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=community_menu_markup())


async def send_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    code = make_ref_code(user.id)
    invite_link = f"https://t.me/{BOT_USERNAME}?start=ref_{code}"

    text = (
        "🚛 *Invite Driver to FleetIQ*\n\n"
        "Share your invite link with another driver.\n\n"
        "They can open FleetIQ Support Bot, join FleetIQ Community and later use FleetIQ Driver App.\n\n"
        "Referral points will be added step by step as the invited driver becomes active.\n\n"
        f"*Your invite link:*\n{invite_link}"
    )

    share_text = (
        "🚛 Join FleetIQ — app + support community for drivers and owner-operators.\n"
        "Track trips, income, expenses, RPM/CPM, settlements and reports."
    )

    # MVP: record link generation. Later use dedicated ReferralEngine stages.
    await post_to_sheets(user_payload(user, {
        "action": "score_add",
        "points": 1,
        "score_action": "invite_link_generated",
        "reason": "Generated invite driver link",
        "context_id": code,
        "source": get_chat_context(update).get("source", ""),
    }))

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=share_invite_markup(invite_link, share_text),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=share_invite_markup(invite_link, share_text),
            disable_web_page_preview=True,
        )


# ── GROUP MODE COMMAND HELPERS ───────────────────────────────────────────────

async def maybe_handle_group_inline_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    msg_type: str,
    missing_hint: str,
) -> bool:
    """Handle /bug text, /idea text, /question text inside group topics.

    Returns True if this update was handled in group mode.
    """
    if not is_group_chat(update):
        return False

    text = command_text(ctx)
    if not text:
        reply = await update.message.reply_text(
            missing_hint,
            parse_mode="Markdown",
        )
        # Try auto-delete hint to reduce group noise. Ignore if permissions fail.
        if ctx.job_queue:
            async def delete_later(context: ContextTypes.DEFAULT_TYPE):
                try:
                    await context.bot.delete_message(chat_id=reply.chat_id, message_id=reply.message_id)
                except Exception as e:
                    log.info("Could not auto-delete hint: %s", redact_secrets(str(e)))
            ctx.job_queue.run_once(delete_later, 10)
        return True

    classification = await classify_feedback(text, msg_type)
    result = await save_structured_feedback(
        update,
        text,
        msg_type,
        classification,
        response="",
        entry_point=f"group_command_{msg_type}",
    )

    if result.get("ok"):
        record_id = str(result.get("record_id", ""))
        short_id = record_id[:8] if record_id else "saved"
        label = {"bug": "Bug", "idea": "Idea", "question": "Question"}.get(msg_type, "Feedback")
        await update.message.reply_text(
            f"✅ {label} saved. Ref: `{short_id}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("⚠️ Could not save this right now. Please try in private support.")
    return True


# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session = current_session(update)
    session["history"] = []
    session["mode"] = None

    start_arg = ctx.args[0] if ctx.args else ""
    session["entry_point"] = start_arg or "direct_start"

    extra = {"start_arg": start_arg}
    source = "telegram_group" if is_group_chat(update) else "telegram_private"

    # Referral deep link: /start ref_u123abc
    if start_arg.startswith("ref_"):
        ref_code = start_arg.replace("ref_", "", 1).strip()
        inviter_id = parse_ref_code(ref_code)
        if inviter_id and inviter_id != user.id:
            extra["inviter_id"] = str(inviter_id)
            extra["invited_by"] = str(inviter_id)
            extra["ref_code"] = ref_code
            await save_user_seen(update, source=source, entry_point="referral_start", extra=extra)

            await update.message.reply_text(
                "👋 Welcome to FleetIQ!\n\n"
                "You were invited by another driver. Open the menu below to join the community, ask a question or try support.",
                reply_markup=main_menu_markup(),
            )
            return

    await save_user_seen(update, source=source, entry_point=start_arg or "direct_start", extra=extra)

    if start_arg == "from_app":
        await send_community_menu(update, ctx)
        return

    if start_arg == "report_bug":
        await cmd_bug(update, ctx)
        return

    if start_arg == "share_idea":
        await cmd_idea(update, ctx)
        return

    if start_arg == "ask_question":
        await cmd_question(update, ctx)
        return

    if start_arg == "invite_driver":
        await send_invite(update, ctx)
        return

    # In group, keep it short and point users to private chat.
    if is_group_chat(update):
        await update.message.reply_text(
            f"👋 FleetIQ Support is active.\n\n"
            f"Private help: https://t.me/{BOT_USERNAME}?start=from_group\n"
            f"Group commands: /bug text, /idea text, /question text, /help",
            disable_web_page_preview=True,
        )
        return

    await send_main_menu(update, ctx)


async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    handled = await maybe_handle_group_inline_command(
        update,
        ctx,
        "feedback",
        "⚠️ Please write feedback after the command. Example:\n`/feedback The app is useful but I need weekly reports`",
    )
    if handled:
        return

    session = current_session(update)
    session["mode"] = "feedback"
    session["entry_point"] = "command_feedback"
    await update.message.reply_text(
        "💬 *General Feedback*\n\nShare any thoughts, comments, or suggestions about FleetIQ:",
        parse_mode="Markdown"
    )


async def cmd_bug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    handled = await maybe_handle_group_inline_command(
        update,
        ctx,
        "bug",
        "⚠️ Please write bug details after the command. Example:\n`/bug Settlement PDF does not export on Android`",
    )
    if handled:
        return

    session = current_session(update)
    session["mode"] = "bug"
    session["entry_point"] = "command_bug"
    await update.message.reply_text(
        "🐛 *Bug Report*\n\nDescribe what happened:\n"
        "• What were you doing?\n"
        "• What went wrong?\n"
        "• What did you expect?\n"
        "• Your device (Android/iPhone/Desktop)?",
        parse_mode="Markdown"
    )


async def cmd_idea(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    handled = await maybe_handle_group_inline_command(
        update,
        ctx,
        "idea",
        "⚠️ Please write your idea after the command. Example:\n`/idea Add detention pay calculator`",
    )
    if handled:
        return

    session = current_session(update)
    session["mode"] = "idea"
    session["entry_point"] = "command_idea"
    await update.message.reply_text(
        "💡 *Feature Idea*\n\nWhat would make FleetIQ better for you?",
        parse_mode="Markdown"
    )


async def cmd_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    handled = await maybe_handle_group_inline_command(
        update,
        ctx,
        "question",
        "⚠️ Please write your question after the command. Example:\n`/question How do I calculate RPM with deadhead?`",
    )
    if handled:
        return

    session = current_session(update)
    session["mode"] = "question"
    session["entry_point"] = "command_question"
    await update.message.reply_text(
        "❓ *Question*\n\nWhat would you like to know about FleetIQ?",
        parse_mode="Markdown"
    )


async def cmd_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            "⭐ Please rate FleetIQ in private chat.",
            reply_markup=private_link_markup("rate_app"),
        )
        return

    keyboard = [[
        InlineKeyboardButton("⭐1", callback_data="rate_1"),
        InlineKeyboardButton("⭐2", callback_data="rate_2"),
        InlineKeyboardButton("⭐3", callback_data="rate_3"),
        InlineKeyboardButton("⭐4", callback_data="rate_4"),
        InlineKeyboardButton("⭐5", callback_data="rate_5"),
    ]]
    await update.message.reply_text(
        "⭐ *Rate FleetIQ Driver*\n\nHow would you rate the app?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            "🎁 Open private chat to get your personal invite link.",
            reply_markup=private_link_markup("invite_driver"),
        )
        return
    await send_invite(update, ctx)


async def cmd_community(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            f"🌐 FleetIQ Community link:\n{COMMUNITY_URL}",
            disable_web_page_preview=True,
        )
        return
    await send_community_menu(update, ctx)


async def cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            "🌍 Choose language in private chat.",
            reply_markup=private_link_markup("language"),
        )
        return
    await update.message.reply_text(
        "🌍 Choose your preferred language:",
        reply_markup=language_menu_markup()
    )


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payload = user_payload(update.effective_user, {
        "action": "get_leaderboard",
        "limit": 10,
        "offset": 0,
        **get_chat_context(update),
    })
    data = await post_to_sheets(payload)

    if not data.get("ok"):
        await update.message.reply_text("Leaderboard is not available yet.")
        return

    rows = data.get("leaderboard", [])
    if not rows:
        await update.message.reply_text("No leaderboard data yet.")
        return

    lines = ["🏆 *FleetIQ Leaderboard*"]
    for idx, row in enumerate(rows, start=1):
        username = row.get("username") or row.get("telegram_id") or "user"
        pts = row.get("points_total", 0)
        rank = row.get("rank", "")
        clean_user = str(username).replace("_", "\\_")
        lines.append(f"{idx}. @{clean_user} — {pts} pts {rank}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            "📬 Contact developer through private support.",
            reply_markup=private_link_markup("contact"),
        )
        return

    await update.message.reply_text(
        "📬 *Contact Developer*\n\n"
        "FleetIQ is built by *SRM Labs*.\n\n"
        "You can reach the team through this bot — just send your message and it will be forwarded.\n\n"
        "Or use /feedback to leave a general message.",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        await update.message.reply_text(
            "📖 *FleetIQ Group Commands*\n\n"
            "/bug text — Report a bug\n"
            "/idea text — Share an idea\n"
            "/question text — Ask a question\n"
            "/leaderboard — Show points\n"
            "/invite — Get private invite link\n"
            "/help — This message\n\n"
            "For full support, open private chat.",
            parse_mode="Markdown",
            reply_markup=private_link_markup("from_group"),
        )
        return

    await update.message.reply_text(
        "📖 *FleetIQ Support Bot Commands*\n\n"
        "/start — Main menu\n"
        "/community — Community & support menu\n"
        "/invite — Invite a driver\n"
        "/bug — Report a bug\n"
        "/idea — Share a feature idea\n"
        "/question — Ask a question\n"
        "/feedback — General feedback\n"
        "/rate — Rate the app\n"
        "/language — Choose language\n"
        "/leaderboard — Show points leaderboard\n"
        "/contact — Contact developer\n"
        "/help — This message\n\n"
        "Or just type your question in private chat.",
        parse_mode="Markdown"
    )


# ── CALLBACK HANDLERS ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    chat = update.effective_chat

    # In group callbacks, do not edit shared group messages. Redirect privately.
    if chat and chat.type in ("group", "supergroup"):
        await query.message.reply_text(
            "Open private FleetIQ Support to use this menu.",
            reply_markup=private_link_markup("from_group_button"),
        )
        return

    session = current_session(update)
    data = query.data

    if data == "community_menu":
        await send_community_menu(update, ctx)
        return

    if data == "invite_driver":
        session["entry_point"] = "callback_invite_driver"
        await send_invite(update, ctx)
        return

    if data == "language_menu":
        await query.edit_message_text(
            "🌍 Choose your preferred language:",
            reply_markup=language_menu_markup()
        )
        return

    if data == "data_share_info":
        await query.edit_message_text(
            "🤝 *Anonymous Data Sharing*\n\n"
            "Soon you will be able to share anonymized FleetIQ data to improve the app and FleetIQ Brain.\n\n"
            "We will not share personal or company data:\n"
            "• no name\n"
            "• no company name\n"
            "• no MC/DOT\n"
            "• no VIN or plate\n"
            "• no exact pickup/dropoff addresses\n\n"
            "Useful anonymous data may include equipment type, load type, RPM, CPM, expenses and income.\n\n"
            "You will be able to preview data before sharing.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")]
            ])
        )
        return

    if data == "back_main":
        await send_main_menu(update, ctx)
        return

    if data.startswith("lang_"):
        lang = data.replace("lang_", "", 1)
        if lang not in SUPPORTED_LANGS:
            await query.edit_message_text("Unsupported language.")
            return

        session["language"] = lang
        await post_to_sheets(user_payload(user, {
            "action": "user_seen",
            "language": lang,
            "language_source": "user_selected",
            "language_confirmed": True,
            "source": "telegram_private",
            "entry_point": "language_selected",
        }))

        await query.edit_message_text(
            f"✅ Language saved: {SUPPORTED_LANGS[lang]}\n\n"
            "I will use this language for private support when possible."
        )
        return

    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        session["mode"] = mode
        session["entry_point"] = f"callback_{mode}"

        prompts = {
            "bug": "🐛 *Bug Report*\n\nDescribe the issue:\n• What happened?\n• What did you expect?\n• Your device?",
            "idea": "💡 *Feature Idea*\n\nWhat would make FleetIQ better for you?",
            "question": "❓ *Question*\n\nWhat would you like to know about FleetIQ?",
            "rate": "⭐ *Rate FleetIQ*\n\nHow would you rate the app?",
            "howto": "📖 Ask me anything about how to use FleetIQ!\n\nFor example:\n• How do I add a load?\n• How does sync work?\n• How to export PDF report?",
        }

        await query.edit_message_text(
            prompts.get(mode, "Go ahead, I'm listening!"),
            parse_mode="Markdown"
        )

        if mode == "rate":
            await ctx.bot.send_message(
                chat_id=user.id,
                text="Tap a number to rate:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("⭐1", callback_data="rate_1"),
                        InlineKeyboardButton("⭐2", callback_data="rate_2"),
                        InlineKeyboardButton("⭐3", callback_data="rate_3"),
                        InlineKeyboardButton("⭐4", callback_data="rate_4"),
                        InlineKeyboardButton("⭐5", callback_data="rate_5"),
                    ]
                ])
            )
        return

    if data.startswith("rate_"):
        stars = int(data.replace("rate_", ""))
        star_str = "⭐" * stars
        session["mode"] = "rate"

        text = f"Rating: {stars}/5 {star_str}"
        classification = await classify_feedback(text, "rating")
        await save_structured_feedback(update, text, "rating", classification, "")

        response = f"Thank you for {star_str}!\n\n"
        if stars <= 3:
            response += "We'd love to know what we can improve. What would make it better?"
            session["mode"] = "feedback"
        else:
            response += "We're glad you're enjoying FleetIQ! 🚛\n\nAnything you'd like to see added?"
            session["mode"] = "idea"

        await query.edit_message_text(response)
        return


# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""
    session = current_session(update)

    # Group mode must never use private-chat mode/state.
    if is_group_chat(update):
        bot_username = BOT_USERNAME.lower().lstrip("@")
        mentioned = f"@{bot_username}" in text.lower()
        replied_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == ctx.bot.id
        )

        # Ignore normal group chatter unless bot is mentioned or user replies to bot.
        if not mentioned and not replied_to_bot:
            return

        text = text.replace(f"@{BOT_USERNAME}", "").replace(f"@{bot_username}", "").strip()
        if not text:
            await update.message.reply_text(
                "How can I help? Use /bug text, /idea text or /question text."
            )
            return

        mode = "question"
        session["mode"] = None
    else:
        mode = session.get("mode") or "question"

    session["history"].append({"role": "user", "content": text})
    if len(session["history"]) > 10:
        session["history"] = session["history"][-10:]

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    response = await ask_claude(session["history"])
    session["history"].append({"role": "assistant", "content": response})

    classification = await classify_feedback(text, mode)
    asyncio.create_task(
        save_structured_feedback(
            update,
            text,
            mode,
            classification,
            response,
            entry_point=session.get("entry_point", ""),
        )
    )

    if classification.get("priority") == "high" and user.id != OWNER_ID:
        await notify_owner(ctx, user, text, classification)

    await update.message.reply_text(response)

    # After capture in private chat, reset transactional modes.
    if not is_group_chat(update) and mode in ("bug", "idea", "feedback", "rate"):
        session["mode"] = None


async def notify_owner(ctx, user, text: str, classification: dict):
    """Send urgent notification to owner."""
    try:
        msg = (
            f"🚨 *High Priority {classification.get('type', '').upper()}*\n\n"
            f"From: @{user.username or user.full_name} (`{user.id}`)\n"
            f"Module: `{classification.get('module', 'unknown')}`\n"
            f"Summary: {classification.get('summary', '')}\n\n"
            f"Message: _{text[:200]}_\n\n"
            f"📋 Prompt:\n`{classification.get('engineering_prompt', '')}`"
        )
        await ctx.bot.send_message(OWNER_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.error("Owner notify error: %s", redact_secrets(str(e)))


# ── DAILY SUMMARY ─────────────────────────────────────────────────────────────

async def send_daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """Send daily summary to owner. Run via job queue."""
    if not SHEETS_URL:
        return

    try:
        now = datetime.now().strftime("%Y-%m-%d")
        msg = (
            f"📊 *FleetIQ Daily Summary — {now}*\n\n"
            f"Bot is running ✅\n"
            f"Community: {COMMUNITY_URL}\n"
            f"Check Google Sheets for today's feedback.\n\n"
            f"_Automated summary from FleetIQ Support Bot_"
        )
        await ctx.bot.send_message(OWNER_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.error("Daily summary error: %s", redact_secrets(str(e)))


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set")
    if not ANTHROPIC_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("community",   cmd_community))
    app.add_handler(CommandHandler("invite",      cmd_invite))
    app.add_handler(CommandHandler("feedback",    cmd_feedback))
    app.add_handler(CommandHandler("bug",         cmd_bug))
    app.add_handler(CommandHandler("idea",        cmd_idea))
    app.add_handler(CommandHandler("question",    cmd_question))
    app.add_handler(CommandHandler("rate",        cmd_rate))
    app.add_handler(CommandHandler("language",    cmd_language))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("contact",     cmd_contact))
    app.add_handler(CommandHandler("help",        cmd_help))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # All text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily summary at 8:00 AM
    if app.job_queue:
        app.job_queue.run_daily(
            send_daily_summary,
            time=time(hour=8, minute=0),
            name="daily_summary"
        )
    else:
        log.warning("JobQueue is not available. Install python-telegram-bot[job-queue] if daily summary is needed.")

    log.info("FleetIQ Support Bot v1.1.1 starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
