from fastapi import FastAPI, Form, Request
from fastapi.responses import Response
import requests, json, sqlite3, logging
from datetime import datetime

app = FastAPI()
DB_PATH = "chat_history.db"

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("bot")

# ---------------- Database helpers ----------------
def init_db():
    """Create the messages table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                message TEXT,
                timestamp TEXT
            )
        """)

def save_message(sender: str, message: str):
    """Insert a new message into the database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages(sender, message, timestamp) VALUES (?,?,?)",
            (sender, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

def get_last_messages(limit: int = 10):
    """Fetch the last N messages (newest first)."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT sender, message FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return cur.fetchall()

# Create table on startup
init_db()

# ---------------- WhatsApp webhook ----------------
@app.post("/webhook")
async def whatsapp_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...)
):
    """
    Twilio calls this endpoint when a WhatsApp message is received.
    We reply with a context-aware answer in <= 6 lines.
    """

    log.info(f"Incoming message from {From}: {Body}")
    # 1️⃣ Save the incoming user message
    save_message(From, Body)

    # 2️⃣ Build conversation history (oldest → newest)
    history = list(reversed(get_last_messages(limit=10)))
    dialogue = []
    for sender, msg in history:
        role = "User" if sender.startswith("whatsapp:") else "Bot"
        dialogue.append(f"{role}: {msg}")
    conversation = "\n".join(dialogue)

    # 3️⃣ Prepare prompt for the local LLM
    user_prompt = (
        "You are a helpful assistant. Read the conversation below and reply "
        "ONLY to the User's most recent message. Keep the answer concise, "
        "within six lines.\n\n"
        f"{conversation}\n\nBot:"
    )

    bot_reply = ""
    try:
        # 4️⃣ Call Ollama with streaming output
        with requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "llama3:latest", "prompt": user_prompt},
            stream=True,
            timeout=60
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                chunk = data.get("response", "")
                if chunk:
                    bot_reply += chunk

        bot_reply = bot_reply.strip() or "Sorry, I couldn’t generate a response."
        if len(bot_reply) > 1500:
            bot_reply = bot_reply[:1500] + "..."

    except Exception as e:
        log.exception("Error while generating response:")
        bot_reply = f"Error: {e}"

    # 5️⃣ Save bot reply to DB
    save_message("bot", bot_reply)
    log.info(f"Bot reply: {bot_reply}")

    # 6️⃣ Twilio requires TwiML (XML) response
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{bot_reply}</Message>
</Response>"""

    # ✅ Return TwiML with correct headers & status
    return Response(content=twiml, media_type="text/xml", status_code=200)

# ---------------- Optional: view chat history ----------------
@app.get("/history")
async def history():
    """View the last 20 messages (for debugging)."""
    rows = get_last_messages(limit=20)
    return {"messages": rows}
