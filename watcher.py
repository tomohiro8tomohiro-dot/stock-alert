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
# Quote (Stooq daily CSV)
# ----------------------------
def fetch_quote_stooq(code: str):
    """
    Stooq（日足CSV）から直近2本の終値を取得
    last       = 最新日の終値（Stooq日足のClose）
    prev_close = その1つ前の終値
    """
    sym = f"{code}.jp"
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()

        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 3:
            print("fetch error", code, "not enough data")
            return None

        row_prev = lines[-2].split(",")
        row_last = lines[-1].split(",")

        prev_close = float(row_prev[4])  # Close
        last = float(row_last[4])        # Close

        return {"last": last, "prev_close": prev_close, "source": "stooq-daily"}
    except Exception as e:
        print("fetch error", code, e)
        return None


# ----------------------------
# LINE Messaging API (Broadcast)
# ----------------------------
def send_line_broadcast(text: str):
    """
    user_id不要。友だち追加している全員に配信（1人でもOK）
    """
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
# notify gate
# ----------------------------
def should_notify(state: dict, key: str, now: datetime, cooldown_minutes: int) -> bool:
    last = state.get(key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last).replace(tzinfo=JST)
    return (now - last_dt) >= timedelta(minutes=cooldown_minutes)


# ----------------------------
# main
# ----------------------------
def main():
    ensure_state_file()

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
    default_market = float(cfg.get("default_percent_market", 10.0))
    default_pts = float(cfg.get("default_percent_pts", 10.0))

    state = load_json(STATE_FILE, {})
    state.setdefault("day_close", {})  # 日中終値の保存場所
    today = now.date().isoformat()

    for code, meta in watch.items():
        name = meta.get("name", code)

        pct = float(
            meta.get("percent_market", default_market) if sess == "market"
            else meta.get("percent_pts", default_pts)
        )

        q = fetch_quote_stooq(code)
        if not q:
            print("skip:", code, "quote unavailable")
            continue

        last = q["last"]
        prev = q["prev_close"]

        # --- day_close 保存（market終盤で保存 / PTS突入時に保険で保存） ---
        saved = state["day_close"].get(code)

        # marketの14:55〜14:59で1回保存
        if sess == "market" and now.hour == 14 and now.minute >= 55:
            if saved is None or saved.get("date") != today:
                state["day_close"][code] = {"date": today, "close": last}
                save_json(STATE_FILE, state)
                print("saved day_close (market):", code, last)

        saved = state["day_close"].get(code)

        # PTSに入ったとき、今日のday_closeが無ければ作る（保険）
        if sess == "pts" and (saved is None or saved.get("date") != today):
            state["day_close"][code] = {"date": today, "close": last}
            save_json(STATE_FILE, state)
            saved = state["day_close"].get(code)
            print("saved day_close (pts fallback):", code, last)

        # --- threshold ---
        if sess == "pts":
            base = saved["close"]
            threshold = base * (1.0 + pct / 100.0)
            base_label = "日中終値"
        else:
            base = prev
            threshold = prev * (1.0 + pct / 100.0)
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
    # テストしたいときだけ、GitHub Actions側で env に LINE_TEST=1 を入れる
    if os.getenv("LINE_TEST", "") == "1":
        send_line_broadcast("✅ LINEテスト: GitHub Actions から送信できています")
        print("LINE_TEST: sent")
    else:
        main()
