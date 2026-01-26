import os, json, time, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests

JST = ZoneInfo("Asia/Tokyo")
STATE_FILE = "state.json"

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def is_weekday(dt_jst: datetime) -> bool:
    return dt_jst.weekday() < 5  # 0=Mon

def parse_hhmm(s: str):
    hh, mm = s.split(":")
    return int(hh), int(mm)

def in_time_range(dt_jst: datetime, start_hm: str, end_hm: str) -> bool:
    sh, sm = parse_hhmm(start_hm)
    eh, em = parse_hhmm(end_hm)
    start = dt_jst.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt_jst.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= dt_jst < end

def session_type(dt_jst: datetime, cfg) -> str:
    tr = cfg["time_rules"]
    if not is_weekday(dt_jst):
        return "off"
    if in_time_range(dt_jst, tr["market_open"], tr["market_close"]):
        return "market"
    if tr.get("use_pts_after_close", False) and dt_jst.hour <= 23:
        # 15:00以降はPTS扱い（ただし価格データがPTSを含むかはソース依存）
        if dt_jst.hour > 15 or (dt_jst.hour == 15 and dt_jst.minute >= 0):
            return "pts"
    return "off"

# --- 価格取得（無料の一般ソースとして Stooq を使用）---
# 注意：この価格がPTSを含むかは保証できません（将来差し替え可能にしてあります）
def fetch_quote_stooq(code: str):
    # Stooqは .jp が使えることが多い（例 7203.jp）
    symbol = f"{code}.jp"
    url = f"https://stooq.com/q/?s={symbol}"
    r = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    html = r.text

    # 現在値（Last）と前日終値（Prev.）っぽい数値を拾う（サイト構造変更に弱いので保険付き）
    def find_value(label):
        # labelの次に出てくる数値っぽいものを拾う（ゆるい）
        m = re.search(rf"{re.escape(label)}.*?([0-9]+(?:\.[0-9]+)?)", html, re.IGNORECASE | re.DOTALL)
        return float(m.group(1)) if m else None

    last = find_value("Last") or find_value("Close")  # どちらか取れれば
    prev = find_value("Prev") or find_value("Prev.") or find_value("Previous")

    if last is None or prev is None:
        raise RuntimeError(f"価格取得に失敗: {code} (last={last}, prev={prev})")

    return {"last": last, "prev_close": prev, "source": "stooq"}

def get_quote(code: str, cfg):
    src = cfg.get("quote_source", "stooq")
    if src == "stooq":
        return fetch_quote_stooq(code)
    raise RuntimeError(f"未対応のquote_source: {src}")

# --- LINE通知（Messaging API Push）---
def send_line(text: str):
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    user_id = os.environ["LINE_USER_ID"]
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"to": user_id, "messages": [{"type":"text", "text": text}]}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()

# --- Mailgun通知（sandbox推奨）---
def send_mailgun(subject: str, text: str):
    api_key = os.environ["MAILGUN_API_KEY"]
    domain = os.environ["MAILGUN_DOMAIN"]     # 例: sandboxXXXX.mailgun.org
    mail_to = os.environ["MAIL_TO"]           # あなたのメール（事前承認が必要）
    mail_from = os.environ.get("MAIL_FROM", f"Stock Alert <postmaster@{domain}>")

    url = f"https://api.mailgun.net/v3/{domain}/messages"
    data = {
        "from": mail_from,
        "to": [mail_to],
        "subject": subject,
        "text": text
    }
    r = requests.post(url, auth=("api", api_key), data=data, timeout=20)
    r.raise_for_status()

def percent_for(code: str, sess: str, cfg) -> float:
    w = cfg["watch"].get(code, {})
    if sess == "market":
        return float(w.get("percent_market", w.get("percent", cfg["default_percent"])))
    if sess == "pts":
        return float(w.get("percent_pts", w.get("percent", cfg["default_percent"])))
    return float(cfg["default_percent"])

def should_notify(state, code: str, now_jst: datetime, cooldown_minutes: int) -> bool:
    last_sent = state.get(code)
    if not last_sent:
        return True
    last_dt = datetime.fromisoformat(last_sent).replace(tzinfo=JST)
    return (now_jst - last_dt) >= timedelta(minutes=cooldown_minutes)

def main():
    cfg = load_json("config.json", {})
    now = datetime.now(JST)
    sess = session_type(now, cfg)
    if sess == "off":
        print("off time")
        return

    state = load_json(STATE_FILE, {})
    cooldown = int(cfg.get("cooldown_minutes", 360))

    for code, meta in cfg["watch"].items():
        name = meta.get("name", code)
        q = get_quote(code, cfg)
        last = q["last"]
        prev = q["prev_close"]
        pct = percent_for(code, sess, cfg)
        threshold = prev * (1.0 + pct/100.0)

        if last >= threshold and should_notify(state, code, now, cooldown):
            tag = "📈【市場】" if sess == "market" else "🌙【PTS】"
            msg = (
                f"{tag}\n"
                f"{code} {name}\n"
                f"前日終値: {prev}\n"
                f"現在値: {last}\n"
                f"+{pct}% 到達\n"
                f"(source: {q['source']})"
            )
            send_line(msg)
            send_mailgun(f"【株価通知】{code} +{pct}% 到達（{ '市場' if sess=='market' else 'PTS' }）", msg)

            state[code] = now.isoformat()
            save_json(STATE_FILE, state)
            print("notified:", code)
        else:
            print("no:", code, last, threshold)

if __name__ == "__main__":
    main()
