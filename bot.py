import asyncio
import json
import os
import re
from bs4 import BeautifulSoup
import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
HISTORY_FILE = "sent_articles.json"

# מילות מפתח מורחבות: קבוצה, צוות וסגל שחקנים מעודכן
KEYWORDS = [
    # שם הקבוצה
    "הפועל ירושלים",
    "הפועל י-ם",
    "Hapoel Jerusalem",
    # צוות מקצועי וניהולי
    "אוברדוביץ",
    "אוברדוביץ'",
    "סאשה אוברדוביץ",
    "Sasa Obradovic",
    "Obradovic",
    "גל מקל",
    "Gal Mekel",
    "מתן אדלסון",
    "Matan Adelson",
    # סגל שחקנים (עברית ואנגלית)
    "ג'ארד הארפר",
    "Jared Harper",
    "שייק מילטון",
    "Shake Milton",
    "ג'יילן סמית",
    "Jaleen Smith",
    "דייויד רודי",
    "David Roddy",
    "קני לופטון",
    "Kenneth Lofton",
    "דבונטה קאקוק",
    "Devontae Cacok",
    "דושאן מילטיץ",
    "Dusan Miletic",
    "רועי הובר",
    "Roi Huber",
    "איתן בורג",
    "Ethan Burg",
    "יובל זוסמן",
    "Yovel Zoosman",
    "נמרוד לוי",
    "Nimrod Levi",
    "יותם חנוכי",
    "Yotam Hanochi",
    "גבי צ'אצ'אשווילי",
    "Gabriel Chachashvili",
]

# RSS Feeds - אתרי ספורט מובילים בישראל ואירופה
RSS_FEEDS = [
    "https://www.one.co.il/cat/coop/xml/rss/newsfeed.aspx?c=3",  # ONE כדורסל
    "https://sport5.co.il/SIP_STORAGE/FEEDS/RSS/2.xml",  # ערוץ הספורט כדורסל
    "https://rss.walla.co.il/feed/155",  # וואלה ספורט
    "https://www.israelhayom.co.il/rss/sport.xml",  # ישראל היום ספורט
    "https://www.eurohoops.net/en/feed/",  # Eurohoops
    "https://www.basketnews.com/rss",  # BasketNews
]


def load_sent_articles():
  if os.path.exists(HISTORY_FILE):
    try:
      with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
    except Exception as e:
      print(f"Error loading history: {e}")
      return set()
  return set()


def save_sent_articles(sent_set):
  with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(list(sent_set), f, ensure_ascii=False, indent=2)


def is_relevant(title, summary):
  text = f"{title} {summary}"
  for kw in KEYWORDS:
    if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
      return True
  return False


def clean_html(raw_html):
  if not raw_html:
    return ""
  soup = BeautifulSoup(raw_html, "html.parser")
  return soup.get_text(separator=" ").strip()


async def fetch_and_send():
  if not TELEGRAM_TOKEN or not CHANNEL_ID:
    print("CRITICAL ERROR: TELEGRAM_TOKEN or CHANNEL_ID is missing!")
    return

  bot = Bot(token=TELEGRAM_TOKEN)
  sent_articles = load_sent_articles()
  new_sent_count = 0

  for feed_url in RSS_FEEDS:
    print(f"Scanning feed: {feed_url}")
    try:
      feed = feedparser.parse(feed_url)
      for entry in feed.entries:
        link = entry.get("link", "")
        title = entry.get("title", "")
        summary = clean_html(entry.get("summary", ""))

        if not link or link in sent_articles:
          continue

        if is_relevant(title, summary):
          msg = (
              f"🏀 **חדשות הפועל ירושלים**\n\n*{title}*\n\n[לקריאת הכתבה"
              f" المלאה]({link})"
          )
          try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=msg,
                parse_mode="Markdown",
            )
            print(f"Sent: {title}")
            sent_articles.add(link)
            new_sent_count += 1
            await asyncio.sleep(2)
          except Exception as e:
            print(f"Failed to send message: {e}")

    except Exception as e:
      print(f"Error parsing feed {feed_url}: {e}")

  if new_sent_count > 0:
    save_sent_articles(sent_articles)
    print(f"Saved {new_sent_count} new articles.")
  else:
    print("No new relevant articles found.")


if __name__ == "__main__":
  asyncio.run(fetch_and_send())
