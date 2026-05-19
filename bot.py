"""
FleetIQ Support Bot v1.0
SRM Labs

Умный Telegram бот для поддержки пользователей FleetIQ.
- Отвечает на вопросы через Claude API
- Классифицирует фидбек (bug/idea/question/complaint)
- Генерирует engineering prompts
- Сохраняет в Google Sheets
- Присылает daily summary владельцу
"""

import os
import json
import logging
import asyncio
from datetime import datetime, time
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── CONFIG ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SHEETS_URL     = os.environ.get("SHEETS_URL", "")
OWNER_ID       = int(os.environ.get("OWNER_ID", "7563117271"))
APP_VERSION    = os.environ.get("APP_VERSION", "1.0")

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

Key facts:
- App URL: srmlabs-dev.github.io/fleetiq-driver
- Works offline (PWA with Service Worker)
- Can be installed on phone from Chrome browser
- Data syncs to Google Sheets via Apps Script URL
- Free to use, no subscription

Your job:
1. Answer user questions about FleetIQ clearly and helpfully
2. Help troubleshoot issues step by step
3. Collect feedback, bugs, and ideas
4. Be concise — users are truck drivers on mobile

Always respond in the same language the user writes in.
If user writes in Russian, respond in Russian.
If user writes in English, respond in English.

For bugs, always ask:
- What device/OS?
- What exactly happened vs what was expected?
- Which part of the app? (loads/pti/sync/reports/settings)

Keep responses short and practical. No fluff."""

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
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "system": system,
                    "messages": messages,
                }
            )
            data = resp.json()
            if "content" in data and data["content"]:
                return data["content"][0]["text"]
            return "Sorry, I couldn't process that. Please try again."
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return "⚠️ AI service temporarily unavailable. Please try again in a moment."


async def classify_feedback(text: str, msg_type: str) -> dict:
    """Ask Claude to classify and create engineering prompt for feedback."""
    prompt = f"""Analyze this user feedback for FleetIQ Driver app.

Type hint from user: {msg_type}
Message: {text}

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "type": "bug|idea|question|complaint",
  "priority": "high|medium|low",
  "module": "loads|pti|sync|reports|settings|disputes|install|other",
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
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                }
            )
            data = resp.json()
            raw = data["content"][0]["text"].strip()
            # Strip markdown fences if present
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        log.error(f"Classify error: {e}")
        return {
            "type": msg_type,
            "priority": "medium",
            "module": "other",
            "summary": text[:80],
            "engineering_prompt": f"User reported: {text[:200]}"
        }

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────

async def save_to_sheets(user, text: str, msg_type: str, classification: dict, response: str):
    """Save feedback to Google Sheets via Apps Script."""
    if not SHEETS_URL:
        log.warning("SHEETS_URL not configured")
        return

    payload = {
        "type": "bot_feedback",
        "timestamp": datetime.now().isoformat(),
        "telegram_id": str(user.id),
        "username": user.username or "",
        "full_name": user.full_name or "",
        "msg_type": msg_type,
        "message": text,
        "fb_type": classification.get("type", ""),
        "priority": classification.get("priority", ""),
        "module": classification.get("module", ""),
        "summary": classification.get("summary", ""),
        "engineering_prompt": classification.get("engineering_prompt", ""),
        "bot_response": response[:500],
        "app_version": APP_VERSION,
        "status": "new",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                SHEETS_URL,
                headers={"Content-Type": "text/plain"},
                content=json.dumps(payload),
            )
        log.info(f"Saved to Sheets: {classification.get('summary', '')}")
    except Exception as e:
        log.error(f"Sheets save error: {e}")

# ── USER SESSION (in-memory, simple) ─────────────────────────────────────────

user_sessions: dict = {}  # user_id → {"history": [], "mode": None}

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {"history": [], "mode": None}
    return user_sessions[user_id]

# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session = get_session(user.id)
    session["history"] = []
    session["mode"] = None

    keyboard = [
        [InlineKeyboardButton("🐛 Report Bug", callback_data="mode_bug"),
         InlineKeyboardButton("💡 Share Idea", callback_data="mode_idea")],
        [InlineKeyboardButton("❓ Ask Question", callback_data="mode_question"),
         InlineKeyboardButton("⭐ Rate App", callback_data="mode_rate")],
        [InlineKeyboardButton("📖 How to Use", callback_data="mode_howto")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Hi {user.first_name}!\n\n"
        f"I'm FleetIQ Support Bot — your assistant for the *FleetIQ Driver* app.\n\n"
        f"I can:\n"
        f"• Answer questions about the app\n"
        f"• Help troubleshoot issues\n"
        f"• Collect your feedback & ideas\n\n"
        f"What can I help you with?",
        parse_mode="Markdown",
        reply_markup=markup
    )


async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["mode"] = "feedback"
    await update.message.reply_text(
        "💬 *General Feedback*\n\nWhat's on your mind? Tell me anything about FleetIQ.",
        parse_mode="Markdown"
    )


async def cmd_bug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["mode"] = "bug"
    await update.message.reply_text(
        "🐛 *Bug Report*\n\nDescribe what happened:\n"
        "• What were you doing?\n"
        "• What went wrong?\n"
        "• Your device (Android/iPhone/Desktop)?",
        parse_mode="Markdown"
    )


async def cmd_idea(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["mode"] = "idea"
    await update.message.reply_text(
        "💡 *Feature Idea*\n\nWhat would make FleetIQ better for you?",
        parse_mode="Markdown"
    )
async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["mode"] = "feedback"
    await update.message.reply_text(
        "💬 *General Feedback*\n\nShare any thoughts, comments, or suggestions about FleetIQ:",
        parse_mode="Markdown"
    )

async def cmd_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("⭐", callback_data="rate_1"),
        InlineKeyboardButton("⭐⭐", callback_data="rate_2"),
        InlineKeyboardButton("⭐⭐⭐", callback_data="rate_3"),
        InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate_4"),
        InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate_5"),
    ]]
    await update.message.reply_text(
        "⭐ *Rate FleetIQ Driver*\n\nHow would you rate the app?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📬 *Contact Developer*\n\n"
        "FleetIQ is built by *SRM Labs*.\n\n"
        "You can reach the team through this bot — just send your message and it will be forwarded.\n\n"
        "Or use /feedback to leave a general message.",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *FleetIQ Support Bot Commands*\n\n"
        "/start — Main menu\n"
        "/bug — Report a bug\n"
        "/idea — Share a feature idea\n"
        "/feedback — General feedback\n"
        "/rate — Rate the app\n"
        "/contact — Contact developer\n"
        "/help — This message\n\n"
        "Or just *type your question* and I'll answer it!",
        parse_mode="Markdown"
    )

# ── CALLBACK HANDLERS ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    session = get_session(user.id)
    data = query.data

    if data.startswith("mode_"):
        mode = data.replace("mode_", "")
        session["mode"] = mode

        prompts = {
            "bug": "🐛 *Bug Report*\n\nDescribe the issue:\n• What happened?\n• What did you expect?\n• Your device?",
            "idea": "💡 *Feature Idea*\n\nWhat would make FleetIQ better for you?",
            "question": "❓ *Question*\n\nWhat would you like to know about FleetIQ?",
            "rate": "⭐ Please use /rate command to rate the app.",
            "howto": "📖 Ask me anything about how to use FleetIQ!\n\nFor example:\n• How do I add a load?\n• How does sync work?\n• How to export PDF report?",
        }
        await query.edit_message_text(
            prompts.get(mode, "Go ahead, I'm listening!"),
            parse_mode="Markdown"
        )

    elif data.startswith("rate_"):
        stars = int(data.replace("rate_", ""))
        star_str = "⭐" * stars
        session["mode"] = "rate"

        text = f"Rating: {stars}/5 {star_str}"
        classification = await classify_feedback(text, "rating")
        await save_to_sheets(user, text, "rating", classification, "")

        response = f"Thank you for {star_str}!\n\n"
        if stars <= 3:
            response += "We'd love to know what we can improve. What would make it better?"
            session["mode"] = "feedback"
        else:
            response += "We're glad you're enjoying FleetIQ! 🚛\n\nAnything you'd like to see added?"
            session["mode"] = "idea"

        await query.edit_message_text(response)

# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""
    session = get_session(user.id)
    mode = session.get("mode") or "question"

    # Add to conversation history
    session["history"].append({"role": "user", "content": text})

    # Keep history to last 10 messages
    if len(session["history"]) > 10:
        session["history"] = session["history"][-10:]

    # Show typing indicator
    await ctx.bot.send_chat_action(user.id, "typing")

    # Get AI response
    response = await ask_claude(session["history"])
    session["history"].append({"role": "assistant", "content": response})

    # Classify and save to Sheets (async, don't wait)
    classification = await classify_feedback(text, mode)
    asyncio.create_task(save_to_sheets(user, text, mode, classification, response))

    # Notify owner for high priority items
    if classification.get("priority") == "high" and user.id != OWNER_ID:
        await notify_owner(ctx, user, text, classification)

    await update.message.reply_text(response)


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
        log.error(f"Owner notify error: {e}")

# ── DAILY SUMMARY ─────────────────────────────────────────────────────────────

async def send_daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """Send daily summary to owner. Run via job queue."""
    if not SHEETS_URL:
        return

    try:
        # Ask Claude to make a summary from recent feedback
        # In production: fetch from Sheets, here we send a status message
        now = datetime.now().strftime("%Y-%m-%d")
        msg = (
            f"📊 *FleetIQ Daily Summary — {now}*\n\n"
            f"Bot is running ✅\n"
            f"Check Google Sheets for today's feedback.\n\n"
            f"_Automated summary from FleetIQ Support Bot_"
        )
        await ctx.bot.send_message(OWNER_ID, msg, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Daily summary error: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set")
    if not ANTHROPIC_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("bug",      cmd_bug))
    app.add_handler(CommandHandler("idea",     cmd_idea))
    app.add_handler(CommandHandler("rate",     cmd_rate))
    app.add_handler(CommandHandler("contact",  cmd_contact))
    app.add_handler(CommandHandler("help",     cmd_help))

    # Callbacks (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # All text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily summary at 8:00 AM
    app.job_queue.run_daily(
        send_daily_summary,
        time=time(hour=8, minute=0),
        name="daily_summary"
    )

    log.info("FleetIQ Support Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
