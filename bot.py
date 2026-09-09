import asyncio
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup
import feedparser
import requests
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
HISTORY_FILE = "sent_hapoel_articles.json"
LAST_HEARTBEAT_FILE = "last_heartbeat.json"

HEARTBEAT_INTERVAL = 21600  # 6 hours

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 1. ביטויים שמזהים בוודאות את הפועל ירושלים בכדורסל
EXACT_MATCH_KEYWORDS = [
    "הפועל ירושלים",
    "הפועל ירשלים",
    "הפועל י\"ם",
    "הפועל ים",
    "הפועל " "בנק יהב" "",
    "hapoel jerusalem",
    "hapoel bank yahav",
]

# 2. מילות מפתח ספציפיות המקשרות לקבוצה (שחקנים/מאמן/הנהלה 2026/27)
SPECIFIC_ENTITIES = [
    "זליקו אוברדוביץ'",
    "ז'ליקו אוברדוביץ'",
    "אוברדוביץ'",
    "מתן אדלסון",
    "אלון קרמר",
    "דייוויד רודי",
    "דיוויד רודי",
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

# 3. מילים שיחד עם "ירושלים" מייצרות זיקה ברורה להפועל ירושלים
BASKETBALL_CONTEXT = [
    "כדורסל",
    "יורוקאפ",
    "eurocup",
    "ליגת ווינר",
    "ארנה",
    "בריגדה",
]

# מקורות RSS
RSS_FEEDS = [
    "https://www.one.co.il/cat/coop/xml/rss/newsfeed.aspx?c=3",
    "https://sport5.co.il/SIP_STORAGE/FEEDS/RSS/2.xml",
    "https://rss.walla.co.il/feed/155",
    "https://www.israelhayom.co.il/rss/sport.xml",
    "https://www.ynet.co.il/Integration/StoryRss3.xml",
    "https://www.maariv.co.il/Rss/RssFeedsSport",
    "https://www.eurohoops.net/en/feed/",
    "https://www.basketnews.com/rss",
    "https://www.euroleaguebasketball.net/eurocup/rss/",
]


def normalize_url(url, base_url):
  """מנקה פרמטרי מעקב (UTM) ומאחד קישורים יחסיים."""
  if not url:
    return ""
  url = urljoin(base_url, url)
  parsed = urlparse(url)
  query = parse_qs(parsed.query)
  # הסרת פרמטרי מעקב נפוצים
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

  # 1. בדיקה של ביטויים מדויקים
  for kw in EXACT_MATCH_KEYWORDS:
    if kw.lower() in text:
      return True

  # 2. בדיקת דמויות/שחקנים ספציפיים
  for entity in SPECIFIC_ENTITIES:
    if entity.lower() in text:
      # לוודא שמוזכרת ירושלים או הפועל בהקשר
      if "ירושלים" in text or "הפועל" in text or "jerusalem" in text:
        return True

  # 3. שילוב בין ירושלים למושגי כדורסל
  if "ירושלים" in text or "jerusalem" in text:
    for context in BASKETBALL_CONTEXT:
      if context.lower() in text:
        return True

  return False


async def fetch_feed(session, feed_url):
  """סריקת פיד אסינכרונית עם טיפול בטיימאאוט."""
  try:
    async with session.get(
        feed_url, headers=FETCH_HEADERS, timeout=15
    ) as response:
      if response.status != 200:
        print(f"Warning: HTTP {response.status} when fetching {feed_url}")
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
    # סריקת כל הפידים במקביל
    tasks = [fetch_feed(session, url) for url in RSS_FEEDS]
    results = await asyncio.gather(*tasks)

    for feed_url, entries in zip(RSS_FEEDS, results):
      for entry in entries:
        raw_link = entry.get("link", "")
        link = normalize_url(raw_link, feed_url)
        title = entry.get("title", "").strip()
        summary_raw = entry.get("summary", "") or entry.get("description", "")
        summary = clean_html(summary_raw)

        if not link or link in sent_articles:
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
            sent_articles.add(link)
            new_sent_count += 1
            await asyncio.sleep(1)  # מניעת חסימת Rate Limit של טלגרם
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
