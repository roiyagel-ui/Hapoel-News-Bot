import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup
import feedparser
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
HISTORY_FILE = "sent_hapoel_articles.json"
LAST_HEARTBEAT_FILE = "last_heartbeat.json"

HEARTBEAT_INTERVAL = 21600  # 6 שעות בשניות
MAX_ARTICLE_AGE_SECONDS = 86400  # התעלמות מכתבות ישנות מ-24 שעות

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 1. ביטויים חיוביים חד-משמעיים להפועל ירושלים בכדורסל
EXACT_MATCH_KEYWORDS = [
    "הפועל ירושלים",
    "הפועל ירשלים",
    "הפועל י\"ם",
    "הפועל ים",
    "הפועל בנק יהב",
    "hapoel jerusalem",
    "hapoel bank yahav",
]

# 2. דמויות ספציפיות מהמועדון (סגל 2026/27)
SPECIFIC_ENTITIES = [
    "אוברדוביץ'",
    "זליקו אוברדוביץ'",
    "ז'ליקו אוברדוביץ'",
    "מתן אדלסון",
    "אלון קרמר",
    "דייוויד רודי",
    "קני לופטון",
    "שייק מילטון",
    "דבונטה קאקוק",
    "ג'יילן סמית'",
    "דושאן מילטיץ'",
    "ג'ארד הארפר",
    "יובל זוסמן",
    "גבי צ'אצ'אשוילי",
    "פיס ארנה",
]

# 3. מילות פסילה (Negative Keywords) - מונע כתבות כדורגל וקבוצות אחרות
NEGATIVE_KEYWORDS = [
    "כדורגל",
    "מכבי חיפה",
    "הפועל חיפה",
    "הפועל תל אביב כדורגל",
    "בית\"ר",
    "ביתר ירושלים",
    "בית\"ר ירושלים",
    "ליגת העל בכדורגל",
    "מכבי תל אביב כדורגל",
    "ליגת האלופות בכדורגל",
    "קונפרנס ליג",
    "פליאוף כדורגל",
]

# מקורות RSS
RSS_FEEDS = [
    "https://www.one.co.il/cat/coop/xml/rss/newsfeed.aspx?c=3",  # ONE כדורסל
    "https://sport5.co.il/SIP_STORAGE/FEEDS/RSS/2.xml",  # ערוץ הספורט כדורסל
    "https://rss.walla.co.il/feed/155",  # וואלה ספורט
    "https://www.israelhayom.co.il/rss/sport.xml",  # ישראל היום ספורט
    "https://www.ynet.co.il/Integration/StoryRss3.xml",  # Ynet ספורט
    "https://www.maariv.co.il/Rss/RssFeedsSport",  # מעריב ספורט
    "https://www.eurohoops.net/en/feed/",  # Eurohoops
    "https://www.basketnews.com/rss",  # BasketNews
    "https://www.euroleaguebasketball.net/eurocup/rss/",  # EuroCup Official
]


def generate_article_id(title, link):
  """יוצר חתימה ייחודית מבוססת כותרת וקישור נקי למניעת כפילויות."""
  clean_title = re.sub(r"\s+", "", title.lower())
  clean_url = urlparse(link).path
  unique_str = f"{clean_title}_{clean_url}"
  return hashlib.md5(unique_str.encode("utf-8")).hexdigest()


def normalize_url(url, base_url):
  if not url:
    return ""
  url = urljoin(base_url, url)
  parsed = urlparse(url)
  query = parse_qs(parsed.query)
  filtered_query = {
      k: v for k, v in query.items() if not k.startswith("utm_")
  }
  new_query = urlencode(filtered_query, doseq=True)
  return urlunparse((
      parsed.scheme,
      parsed.netloc,
      parsed.path,
      parsed.params,
      new_query,
      "",
  ))


def load_sent_articles():
  if os.path.exists(HISTORY_FILE):
    try:
      with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
    except Exception as e:
      print(f"Error loading history: {e}")
  return set()


def save_sent_articles(sent_set):
  with open(HISTORY_FILE, "w", encoding="utf-8") as f:
    json.dump(list(sent_set), f, ensure_ascii=False, indent=2)


def get_last_heartbeat():
  if os.path.exists(LAST_HEARTBEAT_FILE):
    try:
      with open(LAST_HEARTBEAT_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("last_sent", 0)
    except Exception as e:
      print(f"Error loading heartbeat: {e}")
  return 0


def save_last_heartbeat(timestamp):
  with open(LAST_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
    json.dump({"last_sent": timestamp}, f, ensure_ascii=False, indent=2)


def clean_html(raw_html):
  if not raw_html:
    return ""
  return BeautifulSoup(raw_html, "html.parser").get_text(separator=" ").strip()


def is_hapoel_jerusalem_article(title, summary):
  text = f"{title} {summary}".lower()

  # 1. סינון שלילי - אם מופיעה מילת פסילה, הכתבה נפסלת מיד
  for neg_kw in NEGATIVE_KEYWORDS:
    if neg_kw.lower() in text:
      return False

  # 2. בדיקת התאמה מפורשת להפועל ירושלים בכדורסל
  for kw in EXACT_MATCH_KEYWORDS:
    if kw.lower() in text:
      return True

  # 3. בדיקת דמויות/שחקנים בצירוף מפורש של ירושלים או הפועל
  for entity in SPECIFIC_ENTITIES:
    if entity.lower() in text:
      if "ירושלים" in text or "הפועל ירושלים" in text:
        return True

  return False


def is_recent_entry(entry):
  """בודק אם הכתבה פורסמה ב-24 השעות האחרונות."""
  published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
  if published_parsed:
    entry_timestamp = time.mktime(published_parsed)
    if (time.time() - entry_timestamp) > MAX_ARTICLE_AGE_SECONDS:
      return False
  return True


async def fetch_feed(session, feed_url):
  try:
    async with session.get(
        feed_url, headers=FETCH_HEADERS, timeout=15
    ) as response:
      if response.status != 200:
        return []
      content = await response.read()
      feed = feedparser.parse(content)
      return feed.entries
  except Exception as e:
    print(f"Error fetching {feed_url}: {e}")
    return []


async def fetch_and_send():
  if not TELEGRAM_TOKEN or not CHANNEL_ID:
    print("CRITICAL ERROR: TELEGRAM_TOKEN or CHANNEL_ID is missing!")
    return

  bot = Bot(token=TELEGRAM_TOKEN)
  sent_articles = load_sent_articles()
  new_sent_count = 0
  now = time.time()

  async with aiohttp.ClientSession() as session:
    tasks = [fetch_feed(session, url) for url in RSS_FEEDS]
    results = await asyncio.gather(*tasks)

    for feed_url, entries in zip(RSS_FEEDS, results):
      for entry in entries:
        if not is_recent_entry(entry):
          continue

        raw_link = entry.get("link", "")
        link = normalize_url(raw_link, feed_url)
        title = entry.get("title", "").strip()
        summary_raw = entry.get("summary", "") or entry.get("description", "")
        summary = clean_html(summary_raw)

        if not link or not title:
          continue

        article_id = generate_article_id(title, link)
        if article_id in sent_articles:
          continue

        if is_hapoel_jerusalem_article(title, summary):
          msg = (
              f"🔴 **הפועל ירושלים בכדורסל**\n\n*{title}*\n\n[לקריאת הכתבה"
              f" המלאה]({link})"
          )
          try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
            print(f"Sent: {title}")
            sent_articles.add(article_id)
            new_sent_count += 1
            await asyncio.sleep(1)
          except Exception as e:
            print(f"Failed to send message: {e}")

  if new_sent_count > 0:
    save_sent_articles(sent_articles)
    save_last_heartbeat(now)
    print(f"Saved {new_sent_count} new articles.")
  else:
    print("No new relevant articles found for Hapoel Jerusalem.")
    last_heartbeat = get_last_heartbeat()
    if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
      try:
        heartbeat_msg = (
            "🔴 **סריקה תקופתית:** הבוט של הפועל ירושלים פעיל וסרק את כל"
            " מקורות הספורט. לא נמצאו כתבות חדשות ב-6 השעות האחרונות."
        )
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=heartbeat_msg,
            parse_mode="Markdown",
        )
        save_last_heartbeat(now)
        print("Sent 6-hour heartbeat status message.")
      except Exception as e:
        print(f"Failed to send heartbeat message: {e}")


if __name__ == "__main__":
  asyncio.run(fetch_and_send())
