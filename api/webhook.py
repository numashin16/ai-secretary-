from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import anthropic
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("calendar", "v3", credentials=creds)


def parse_with_ai(message: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": f"""今日は{today}です。以下のメッセージを解析してJSONのみ返してください。

メッセージ: {message}

{{
  "action": "add" | "list" | "delete" | "unknown",
  "title": "予定のタイトル（簡潔に整理したもの）",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM or null",
  "description": "補足情報があれば"
}}

ルール:
- actionが「add」: 予定の登録
- actionが「list」: 予定の確認（今日・明日・特定日）
- titleは簡潔に整理する（例: 「明日の14時に田中さんとミーティング」→「田中さんとミーティング」）
- 終了時間が不明な場合はnull（1時間として登録）
- 情報が不足してaddできない場合はunknown""",
            }
        ],
    )
    return json.loads(response.content[0].text)


def add_event(info: dict) -> str:
    service = get_calendar_service()
    start_time = info["start_time"]
    end_time = info.get("end_time") or (
        datetime.strptime(start_time, "%H:%M") + timedelta(hours=1)
    ).strftime("%H:%M")

    event = {
        "summary": info["title"],
        "description": info.get("description", ""),
        "start": {
            "dateTime": f"{info['date']}T{start_time}:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": f"{info['date']}T{end_time}:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
    }
    service.events().insert(calendarId="primary", body=event).execute()
    return f"登録しました！\n\n📅 {info['title']}\n🕐 {info['date']} {start_time}〜{end_time}"


def list_events(date: str) -> str:
    service = get_calendar_service()
    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=f"{date}T00:00:00+09:00",
            timeMax=f"{date}T23:59:59+09:00",
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    if not events:
        return f"{date}の予定はありません。"

    lines = [f"📅 {date}の予定"]
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        time_str = start[11:16] if "T" in start else "終日"
        lines.append(f"・{time_str} {e['summary']}")
    return "\n".join(lines)


@app.route("/api/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    try:
        info = parse_with_ai(user_message)
        action = info.get("action")

        if action == "add" and info.get("title") and info.get("date") and info.get("start_time"):
            reply = add_event(info)
        elif action == "list":
            date = info.get("date") or datetime.now().strftime("%Y-%m-%d")
            reply = list_events(date)
        else:
            reply = (
                "こんな感じで送ってください！\n\n"
                "予定を登録：「明日14時 田中さんとミーティング」\n"
                "予定を確認：「今日の予定は？」「明日の予定」"
            )
    except Exception as e:
        reply = f"エラーが発生しました🙏\n{str(e)}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


if __name__ == "__main__":
    app.run(port=8000, debug=True)
