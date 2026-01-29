from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

def send_line(msg):
    # ※ここは「いま本番で使っている send_line」をそのまま使ってください
    print("LINE:", msg)

def main():
    now = datetime.now(JST)
    send_line(
        "🧪 テスト通知\n"
        f"時刻: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "→ LINE連携チェック"
    )

if __name__ == "__main__":
    main()
