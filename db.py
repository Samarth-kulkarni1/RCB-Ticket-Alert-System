"""
db.py  –  MongoDB helpers for the RCB ticket scraper.

Collections
-----------
users       : { chat_id: int, subscribed: bool, joined_at: datetime }
alert_log   : { url: str, status: str, alerted_at: datetime }
"""

from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

# ── Connection ────────────────────────────────────────────────────────────────

MONGO_URI = "mongodb://localhost:27017"   # override via env-var if needed
DB_NAME   = "rcb_scraper"

_client: MongoClient | None = None


def get_db():
    """Return the database handle, creating the client on first call."""
    global _client
    
    if _client is None:
        import os
        uri = os.getenv("MONGO_URI", MONGO_URI)
        _client = MongoClient(uri, serverSelectionTimeoutMS=5_000)
    return _client[DB_NAME]


def init_db():
    """Create indexes (idempotent – safe to call on every startup)."""
    db = get_db()
    db.users.create_index("chat_id", unique=True)
    db.alert_log.create_index([("url", ASCENDING), ("alerted_at", ASCENDING)])


# ── User management ───────────────────────────────────────────────────────────

def add_user(chat_id: int) -> bool:
    """
    Subscribe a user.  Returns True if they are *newly* added, False if they
    were already in the collection (re-subscribe sets subscribed=True).
    """
    db = get_db()
    existing = db.users.find_one({"chat_id": chat_id})

    if existing is None:
        db.users.insert_one({
            "chat_id":    chat_id,
            "subscribed": True,
            "joined_at":  datetime.now(timezone.utc),
        })
        return True   # brand-new user

    if not existing.get("subscribed"):
        db.users.update_one(
            {"chat_id": chat_id},
            {"$set": {"subscribed": True}},
        )
    return False      # already existed


def remove_user(chat_id: int) -> bool:
    """
    Unsubscribe a user (soft-delete – keeps the document).
    Returns True if the user was found and updated.
    """
    db = get_db()
    result = db.users.update_one(
        {"chat_id": chat_id},
        {"$set": {"subscribed": False}},
    )
    return result.matched_count > 0


def get_subscribed_ids() -> list[int]:
    """Return all chat_ids that are currently subscribed."""
    db = get_db()
    return [doc["chat_id"] for doc in db.users.find({"subscribed": True}, {"chat_id": 1})]


def user_count() -> int:
    """Return the number of active subscribers."""
    return get_db().users.count_documents({"subscribed": True})


# ── Alert log ─────────────────────────────────────────────────────────────────

def log_alert(url: str, status: str):
    """Persist an alert event so we can avoid duplicate broadcasts."""
    get_db().alert_log.insert_one({
        "url":        url,
        "status":     status,
        "alerted_at": datetime.now(timezone.utc),
    })


def was_alert_sent(url: str) -> bool:
    """
    Return True if an alert for this URL has already been logged
    *and* no CLOSED/UNKNOWN event has been logged since.
    """
    db = get_db()
    last = db.alert_log.find_one(
        {"url": url},
        sort=[("alerted_at", -1)],
    )
    if last is None:
        return False
    return last["status"] == "OPEN"


def clear_alert(url: str):
    """Record that the URL is no longer OPEN (resets the alert gate)."""
    log_alert(url, "CLOSED")