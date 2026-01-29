import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"


# ----------------------------
# util
# ----------------------------
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def ensure_state_file():
    if not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, {})


def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # 0=Mon ... 4=Fri


def in_range(dt: datetime, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    s = dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    e = dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return s <= dt < e


def session_type(now: datetime) -> str:
    """
    market: 09:00-15:00 (JST)
    pts   : 15:00-23:00 (JST)
    off   : それ以外
    """
    if not is_weekday(now):
        return "off"
    if in_range(now, 9, 0, 15, 0):
        return "market"
    if in_range(now, 15, 0, 23, 0):
        return "pts"
    return "off"


# ----------------------------
# Quote (Yahoo Finance 非公式エンドポイント)
# ----------------------------
def fetch_quote_yahoo(code: str):
    """
    Yahoo Financeの quote から
    - regularMarketPrice
    - postMarketPrice (あれば)
    - regularMarketPreviousClose
    を取得
    """
    sym = f"{code}.T"  # 東証は .T
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": sym}
    try:
        r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        result = (data.get("quoteResponse", {}) or {}).get("result", []) or []
        if not result:
            return None

        q = result[0]
        regular = q.get("regularMarketPrice")
        post = q.get("postMarketPrice")
        prev_close = q.get("regularMarketPreviousClose")

        # 数値が取れないときはNoneのまま返す
        return {
            "regular": float(regular) if regular is not None else None,
            "post": float(post) if post is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "source": "yahoo-quote",
        }
    except Exception as e:
        print("fetch error (yahoo)", code, e)
        return None


# ----------------------------
# LINE Messaging API (Broadcast)
# ----------------------------
def send_line_broadcast(text: str):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が未設定です（GitHub Secrets）")

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text}]}

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"LINE送信失敗: {r.status_code} {r.text}")
    return True


# ----------------------------
# main logic
# ----------------------------
def should_notify(state: dict, key: str, now: datetime, cooldown_minutes: int) -> bool:
    last = state.get(key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last).replace(tzinfo=JST)
    return (now - last_dt) >= timedelta(minutes=cooldown_minutes)


def main():
    ensure_state_file()

    # LINE_TEST=1 のときだけテスト送信（運用時は送らない）
    if os.getenv("LINE_TEST", "") == "1":
        send_line_broadcast("✅ LINEテスト: stock-alert から送信できています")
        print("LINE_TEST: sent")
        return

    cfg = load_json(CONFIG_FILE, {})
    watch = cfg.get("watch", {})
    if not watch:
        print("watch が空です。config.json に watch を入れてください。")
        return

    now = datetime.now(JST)
    sess = session_type(now)
    if sess == "off":
        print("off time")
        return

    cooldown = int(cfg.get("cooldown_minutes", 360))
    default_market = float(cfg.get("default_percent_market", 2.0))
    default_pts = float(cfg.get("default_percent_pts", 10.0))

    state = load_json(STATE_FILE, {})

    for code, meta in watch.items():
        name = meta.get("name", code)
        pct = float(
            meta.get("percent_market", default_market) if sess == "market"
            else meta.get("percent_pts", default_pts)
        )

        q = fetch_quote_yahoo(code)
        if not q:
            print("skip:", code, "quote unavailable")
            continue

        # 市場: regular を使う / PTS: post があれば優先
        last = q["regular"] if sess == "market" else (q["post"] if q["post"] is not None else q["regular"])
        prev = q["prev_close"]

        if last is None or prev is None:
            print("skip:", code, "missing price/prev_close", q)
            continue

        base = prev
        threshold = base * (1.0 + pct / 100.0)
        base_label = "前日終値"

        state_key = f"{code}:{sess}:{pct}"

        if last >= threshold and should_notify(state, state_key, now, cooldown):
            tag = "📈【市場】" if sess == "market" else "🌙【PTS】"
            msg = (
                f"{tag}\n"
                f"{code} {name}\n"
                f"{base_label}: {base}\n"
                f"現在値: {last}\n"
                f"+{pct}% 到達\n"
                f"(source: {q['source']})"
            )
            send_line_broadcast(msg)
            state[state_key] = now.isoformat()
            save_json(STATE_FILE, state)
            print("notified:", code, last, ">=", threshold)
        else:
            print("no:", code, last, "<", threshold)


if __name__ == "__main__":
    main()
