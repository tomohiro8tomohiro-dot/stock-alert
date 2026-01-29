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
    return dt.weekday() < 5


def in_range(dt: datetime, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    s = dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    e = dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return s <= dt < e


def session_type(now: datetime) -> str:
    """
    market only (JST): 09:00-15:00
    off: otherwise
    """
    if not is_weekday(now):
        return "off"
    if in_range(now, 9, 0, 15, 0):
        return "market"
    return "off"


# ----------------------------
# Quote (Stooq quote CSV: free, "near real-time")
# ----------------------------
def fetch_quote_stooq(code: str):
    """
    Stooq quote CSV
    last      : Close（現在値に近い値として扱う）
    prev_close: PrevClose（前日終値）
    """
    sym = f"{code}.jp"
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()

        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2:
            print("fetch error", code, "not enough data")
            return None

        headers = lines[0].split(",")
        values = lines[1].split(",")
        row = dict(zip(headers, values))

        last = float(row["Close"])
        prev_close = float(row["PrevClose"])

        return {"last": last, "prev_close": prev_close, "source": "stooq-quote"}
    except Exception as e:
        print("fetch error", code, e)
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

    # テスト送信（必要なときだけ）
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
    if session_type(now) == "off":
        print("off time (market only)")
        return

    cooldown = int(cfg.get("cooldown_minutes", 360))

    # 上昇/下落 どちらも10%をデフォルトに
    default_up = float(cfg.get("default_percent_up", 10.0))
    default_down = float(cfg.get("default_percent_down", 10.0))

    state = load_json(STATE_FILE, {})

    for code, meta in watch.items():
        name = meta.get("name", code)
        up_pct = float(meta.get("percent_up", default_up))
        down_pct = float(meta.get("percent_down", default_down))

        q = fetch_quote_stooq(code)
        if not q:
            print("skip:", code, "quote unavailable")
            continue

        last = q["last"]
        prev = q["prev_close"]

        up_th = prev * (1.0 + up_pct / 100.0)
        down_th = prev * (1.0 - down_pct / 100.0)

        # 上昇通知
        up_key = f"{code}:market:up:{up_pct}"
        if last >= up_th and should_notify(state, up_key, now, cooldown):
            msg = (
                f"📈【市場 上昇】\n"
                f"{code} {name}\n"
                f"前日終値: {prev}\n"
                f"現在値: {last}\n"
                f"+{up_pct}% 到達\n"
                f"(source: {q['source']})"
            )
            send_line_broadcast(msg)
            state[up_key] = now.isoformat()
            save_json(STATE_FILE, state)
            print("notified UP:", code, last, ">=", up_th)
        else:
            print("no UP:", code, last, "<", up_th)

        # 下落通知
        down_key = f"{code}:market:down:{down_pct}"
        if last <= down_th and should_notify(state, down_key, now, cooldown):
            msg = (
                f"📉【市場 下落】\n"
                f"{code} {name}\n"
                f"前日終値: {prev}\n"
                f"現在値: {last}\n"
                f"-{down_pct}% 到達\n"
                f"(source: {q['source']})"
            )
            send_line_broadcast(msg)
            state[down_key] = now.isoformat()
            save_json(STATE_FILE, state)
            print("notified DOWN:", code, last, "<=", down_th)
        else:
            print("no DOWN:", code, last, ">", down_th)


if __name__ == "__main__":
    main()
