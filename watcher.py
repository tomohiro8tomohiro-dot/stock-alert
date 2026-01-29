import json
import requests
from datetime import datetime, timezone, timedelta

# ===== 設定 =====
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

JST = timezone(timedelta(hours=9))


# ===== util =====
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def send_line(msg):
    # ここは「あなたが既に動かしている send_line」をそのまま使ってOK
    # 例:
    # requests.post(LINE_URL, headers=..., data=...)
    print("LINE:", msg)


def fetch_quote_stooq(code):
    """
    Stooq を使った簡易株価取得
    """
    url = f"https://stooq.com/q/l/?s={code}.jp&f=sd2t2ohlcv&h&e=json"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()["data"][0]
        last = float(data["c"])
        prev = float(data["pc"])
        return {"last": last, "prev_close": prev}
    except Exception as e:
        print("fetch error", code, e)
        return None


# ===== main =====
def main():
    cfg = load_json(CONFIG_FILE, {})
    watch = cfg.get("watch", {})
    default_pct = float(cfg.get("default_percent_market", 10.0))

    state = load_json(STATE_FILE, {})
    state.setdefault("levels", {})

    now = datetime.now(JST)

    print("STATE:", state)

    for code, meta in watch.items():
        name = meta.get("name", code)

        q = fetch_quote_stooq(code)
        if not q:
            continue

        last = q["last"]
        prev = q["prev_close"]
        if not last or not prev:
            continue

        # 10% / 20% 段階
        levels = [10, 20]
        notified = state["levels"].setdefault(code, [])

        for lv in levels:
            up_th = prev * (1.0 + lv / 100.0)
            down_th = prev * (1.0 - lv / 100.0)

            # 上昇
            if last >= up_th and lv not in notified:
                send_line(
                    f"📈 {code} {name}\n"
                    f"前日比 +{lv}% 到達\n"
                    f"{prev:.0f} → {last:.0f}"
                )
                notified.append(lv)
                save_json(STATE_FILE, state)

            # 下落
            if last <= down_th and -lv not in notified:
                send_line(
                    f"📉 {code} {name}\n"
                    f"前日比 −{lv}% 到達\n"
                    f"{prev:.0f} → {last:.0f}"
                )
                notified.append(-lv)
                save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
