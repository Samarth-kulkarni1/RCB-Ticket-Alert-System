import requests
from bs4 import BeautifulSoup
import time
import logging
from datetime import datetime
import re
import sys

import db  # ← MongoDB helper

import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

sys.stdout.reconfigure(encoding="utf-8")


LAST_UPDATE_ID = None

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("rcb_tickets.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

URLS_TO_MONITOR = [
    {"name": "RCB Shop Ticket Page", "url": "https://shop.royalchallengers.com/ticket"},
    {"name": "RCB Official Website",  "url": "https://www.royalchallengers.com/"},
]

UPCOMING_RCB_HOME_MATCHES = [
    {"date": "2026-04-24", "match": "RCB vs GT",  "platform": "RCB website/app"},
    {"date": "2026-05-10", "match": "RCB vs MI",  "platform": "RCB website/app"},
    {"date": "2026-05-13", "match": "RCB vs KKR", "platform": "RCB website/app"},
]

CHECK_INTERVAL_SECONDS = 20
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

last_status: dict[str, str] = {}

# ── Telegram helpers ──────────────────────────────────────────────────────────

def send_message(chat_id: int, message: str):
    try:
        url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        log.warning("Telegram send failed: " + str(e))


def broadcast(message: str):
    for chat_id in db.get_subscribed_ids():
        send_message(chat_id, message)


def update_users():
    """Poll Telegram for /start and /stop commands; persist changes to MongoDB."""
    global LAST_UPDATE_ID

    url    = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {}
    if LAST_UPDATE_ID:
        params["offset"] = LAST_UPDATE_ID + 1

    try:
        resp = requests.get(url, params=params, timeout=10).json()
    except Exception as e:
        log.warning("Failed to poll Telegram: " + str(e))
        return

    for result in resp.get("result", []):
        LAST_UPDATE_ID = result["update_id"]

        msg = result.get("message")
        if not msg:
            continue

        chat_id = msg["chat"]["id"]
        text    = msg.get("text", "").strip().lower()

        if text == "/start":
            is_new = db.add_user(chat_id)
            send_message(chat_id, " You are subscribed to RCB ticket alerts!")
            if is_new:
                log.info(f"New subscriber: {chat_id}  |  total active: {db.user_count()}")
                # Notify admins / log that a new user just joined
               

        elif text == "/stop":
            db.remove_user(chat_id)
            send_message(chat_id, "You are unsubscribed.")
            log.info(f"Unsubscribed: {chat_id}  |  total active: {db.user_count()}")


# ── Scraping helpers ──────────────────────────────────────────────────────────

def fetch_page(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning("Failed to fetch " + url + " : " + str(e))
        return None


def check_availability(soup) -> dict:
    page_text = soup.get_text(separator=" ").lower()

    if any(phrase in page_text for phrase in [
        "tickets not available.",
        "please await further announcements.",
        "sold out",
    ]):
        return {"status": "CLOSED"}

    if "buy ticket" in page_text or "book now" in page_text:
        return {"status": "OPEN"}

    return {"status": "UNKNOWN"}


def extract_ticket_links(soup, base_url: str) -> list[dict]:
    links = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if any(kw in text for kw in ["ticket", "book", "buy"]):
            href = a["href"] if a["href"].startswith("http") else base_url + a["href"]
            links.append({"text": a.get_text(strip=True), "href": href})
    return links


def extract_match_info(soup) -> list[str]:
    matches: list[str] = []
    pattern = re.compile(r"RCB\s+vs\s+\w+", re.IGNORECASE)
    for match in pattern.findall(soup.get_text()):
        if match not in matches:
            matches.append(match)
    return matches


def alert(message: str, url: str):
    border = "=" * 60
    log.info("\n" + border)
    log.info("ALERT: " + message)
    log.info(border)
    broadcast(" RCB Tickets LIVE!\n\n" + message)
    db.log_alert(url, "OPEN")


def print_upcoming_matches():
    today = datetime.today().date()
    log.info("\nUpcoming RCB Home Matches:")
    for m in UPCOMING_RCB_HOME_MATCHES:
        match_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
        days_left  = (match_date - today).days
        days_status = f"in {days_left} days" if days_left >= 0 else "past"
        log.info(
            "   " + m["date"] + " | " + m["match"].ljust(14) +
            " | Platform: " + m["platform"].ljust(22) +
            " | " + days_status
        )
    log.info("")


# ── Main scrape cycle ─────────────────────────────────────────────────────────

def scrape_once():
    for target in URLS_TO_MONITOR:
        name = target["name"]
        url  = target["url"]
        log.info("Checking: " + name + "  ->  " + url)

        soup = fetch_page(url)
        if not soup:
            continue

        result  = check_availability(soup)
        links   = extract_ticket_links(soup, url)
        matches = extract_match_info(soup)

        log.info("  Status           : " + result["status"])
        if matches:
            log.info("  Matches detected  : " + str(matches))
        if links:
            log.info("  Ticket links found:")
            for lnk in links[:5]:
                log.info("       [" + lnk["text"] + "]  ->  " + lnk["href"])

        if result["status"] == "OPEN" and not db.was_alert_sent(url):
            alert("Tickets LIVE: " + name + " -> " + url, url)

        if result["status"] != "OPEN":
            db.clear_alert(url)

        last_status[url] = result["status"]


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    db.init_db()                          # ensure indexes exist
    log.info("RCB Ticket Scraper Started")
    log.info(f"Checking every {CHECK_INTERVAL_SECONDS}s | Ctrl+C to stop\n")
    log.info(f"Active subscribers on startup: {db.user_count()}")
    print_upcoming_matches()

    iteration = 1
    while True:
        update_users()
        log.info(
            "-- Iteration #" + str(iteration) +
            "  [" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] --"
        )
        scrape_once()
        log.info("Sleeping " + str(CHECK_INTERVAL_SECONDS) + "s\n")
        time.sleep(CHECK_INTERVAL_SECONDS)
        iteration += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nScraper stopped by user. Go Challengers!")