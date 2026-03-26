from http.server import BaseHTTPRequestHandler
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import anthropic
import json
import os
from datetime import datetime, timedelta


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
- titleは簡潔に整理する
- 終了時間が不明な場合はnull
- 情報不足でaddできない場合はunknown""",
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
    return f"登録しました！\n\n{info['title']}\n{info['date']} {start_time}〜{end_time}"


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

    lines = [f"{date}の予定"]
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        time_str = start[11:16] if "T" in start else "終日"
        lines.append(f"・{time_str} {e['summary']}")
    return "\n".join(lines)


def handle_message_logic(user_message: str) -> str:
    try:
        info = parse_with_ai(user_message)
        action = info.get("action")

        if action == "add" and info.get("title") and info.get("date") and info.get("start_time"):
            return add_event(info)
        elif action == "list":
            date = info.get("date") or datetime.now().strftime("%Y-%m-%d")
            return list_events(date)
        else:
            return (
                "こんな感じで送ってください！\n\n"
                "予定を登録：「明日14時 田中さんとミーティング」\n"
                "予定を確認：「今日の予定は？」「明日の予定」"
            )
    except Exception as e:
        return f"エラーが発生しました\n{str(e)}"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        signature = self.headers.get("X-Line-Signature", "")

        try:
            @handler.add(MessageEvent, message=TextMessage)
            def handle_message(event):
                reply = handle_message_logic(event.message.text)
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=reply)
                )

            handler.handle(body, signature)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except InvalidSignatureError:
            self.send_response(400)
            self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AI Secretary is running!")
