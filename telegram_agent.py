import asyncio
import yaml
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from core.session import MultiMCP
from core.loop import AgentLoop
from dotenv import load_dotenv
import os

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_KEY")

# Load profile once at startup
with open("config/profiles.yaml", "r") as f:
    profile = yaml.safe_load(f)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called every time someone sends a message to your bot."""
    
    user_message = update.message.text
    chat_id = update.effective_chat.id
    print(f"[telegram] Received: {user_message}")
    
    # Tell user we're working on it
    await update.message.reply_text("🔄 Processing your query...")
    
    try:
        # Initialize MCP (same as agent.py does)
        mcp_servers = profile.get("mcp_servers", [])
        mcp = MultiMCP(mcp_servers)
        await mcp.initialize()
        # Create and run the AgentLoop (same as agent.py)
        agent = AgentLoop(user_message, mcp)
        result = await agent.run()
        
        # Send the result back to Telegram
        # Strip the "FINAL_ANSWER:" prefix for cleaner output
        answer = result.replace("FINAL_ANSWER:", "").strip()
        await update.message.reply_text(f"✅ {answer}")
        
        print(f"[telegram] Sent answer: {answer}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        print(f"[telegram] Error: {e}")

def main():
    print("[telegram] Starting bot...")
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Listen for any text message (not commands like /start)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("[telegram] Bot is running. Send a message on Telegram!")
    app.run_polling()

if __name__ == "__main__":
    main()