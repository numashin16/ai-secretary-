from http.server import BaseHTTPRequestHandler
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, FileMessage, TextSendMessage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from notion_client import Client as NotionClient
import anthropic
import pdfplumber
import json
import os
import io
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

NOTION_TASKS_DB = "333e9d8090cd81a1bd36f5f2b5925431"
NOTION_CALENDAR_DB = "333e9d8090cd81739bc4f12f57891645"
NOTION_PAPERS_DB = "333e9d8090cd8198a958f127a0383ff6"


def now_jst():
    return datetime.now(JST)


def get_line_bot_api():
    return LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])


def get_line_handler():
    return WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])


def get_claude():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def get_notion():
    return NotionClient(auth=os.environ["NOTION_TOKEN"])


def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("calendar", "v3", credentials=creds)


# ── AI解析 ──────────────────────────────────────────────

def parse_with_ai(message: str) -> dict:
    today = now_jst().strftime("%Y-%m-%d")
    claude = get_claude()
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": f"""今日は{today}です。以下のメッセージを解析してJSONのみ返してください。

メッセージ: {message}

{{
  "action": "add_event" | "list_events" | "delete_event" | "add_task" | "list_tasks" | "complete_task" | "unknown",
  "title": "タイトル（簡潔に）",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM or null",
  "description": "補足情報",
  "due": "YYYY-MM-DD or null（タスクの期限）"
}}

ルール:
- add_event: 予定の登録
- list_events: 予定の確認
- delete_event: 予定の削除
- add_task: タスクの追加（「タスク」「やること」「TODO」などのキーワード）
- list_tasks: タスクの一覧（「タスク確認」「やること一覧」など）
- complete_task: タスクの完了（「〇〇完了」「〇〇終わった」など）
- titleは簡潔に整理する
- 終了時間・期限が不明な場合はnull
- 情報不足な場合はunknown""",
            }
        ],
    )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ── Google Calendar ──────────────────────────────────────

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

    # Notionにも同期
    sync_event_to_notion(info["title"], info["date"], start_time, end_time, info.get("description", ""))

    return f"登録しました！\n\n{info['title']}\n{info['date']} {start_time}〜{end_time}"


def sync_event_to_notion(title: str, date: str, start_time: str, end_time: str, description: str):
    notion = get_notion()
    notion.pages.create(
        parent={"database_id": NOTION_CALENDAR_DB},
        properties={
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": f"{date}T{start_time}:00+09:00", "end": f"{date}T{end_time}:00+09:00"}},
            "Description": {"rich_text": [{"text": {"content": description}}]},
        },
    )


def delete_event(info: dict) -> str:
    service = get_calendar_service()
    date = info.get("date") or now_jst().strftime("%Y-%m-%d")
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
        return f"{date}に削除できる予定がありません。"

    title = info.get("title", "")
    start_time = info.get("start_time", "")
    target = None
    for e in events:
        summary = e.get("summary", "")
        start = e["start"].get("dateTime", "")
        event_time = start[11:16] if "T" in start else ""
        if title and title in summary:
            target = e
            break
        if start_time and event_time == start_time:
            target = e
            break

    if not target:
        lines = [f"{date}の予定一覧（削除したい予定を「〇〇を削除して」と送ってください）"]
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            time_str = start[11:16] if "T" in start else "終日"
            lines.append(f"・{time_str} {e['summary']}")
        return "\n".join(lines)

    service.events().delete(calendarId="primary", eventId=target["id"]).execute()
    return f"削除しました！\n\n{target['summary']}"


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


# ── Notion タスク ────────────────────────────────────────

def add_task(info: dict) -> str:
    notion = get_notion()
    props = {
        "Name": {"title": [{"text": {"content": info["title"]}}]},
        "Status": {"select": {"name": "未着手"}},
    }
    if info.get("due"):
        props["Due"] = {"date": {"start": info["due"]}}
    if info.get("description"):
        props["Notes"] = {"rich_text": [{"text": {"content": info["description"]}}]}

    notion.pages.create(parent={"database_id": NOTION_TASKS_DB}, properties=props)
    due_str = f"\n期限: {info['due']}" if info.get("due") else ""
    return f"タスクを追加しました！\n\n{info['title']}{due_str}"


def list_tasks() -> str:
    notion = get_notion()
    response = notion.databases.query(
        database_id=NOTION_TASKS_DB,
        filter={"property": "Status", "select": {"does_not_equal": "完了"}},
        sorts=[{"property": "Due", "direction": "ascending"}],
    )
    pages = response.get("results", [])
    if not pages:
        return "未完了のタスクはありません。"

    lines = ["未完了タスク一覧"]
    for p in pages:
        title = p["properties"]["Name"]["title"]
        name = title[0]["text"]["content"] if title else "(無題)"
        status = p["properties"]["Status"]["select"]["name"] if p["properties"]["Status"]["select"] else "未着手"
        due = p["properties"]["Due"]["date"]["start"][:10] if p["properties"]["Due"]["date"] else ""
        due_str = f" [{due}]" if due else ""
        lines.append(f"・{name}{due_str} ({status})")
    return "\n".join(lines)


def complete_task(info: dict) -> str:
    notion = get_notion()
    response = notion.databases.query(
        database_id=NOTION_TASKS_DB,
        filter={"property": "Status", "select": {"does_not_equal": "完了"}},
    )
    pages = response.get("results", [])
    title_query = info.get("title", "")
    target = None
    for p in pages:
        title_list = p["properties"]["Name"]["title"]
        name = title_list[0]["text"]["content"] if title_list else ""
        if title_query and title_query in name:
            target = p
            break

    if not target:
        return f"「{title_query}」というタスクが見つかりませんでした。\nタスク一覧を確認してください。"

    notion.pages.update(
        page_id=target["id"],
        properties={"Status": {"select": {"name": "完了"}}},
    )
    title_list = target["properties"]["Name"]["title"]
    name = title_list[0]["text"]["content"] if title_list else ""
    return f"完了にしました！\n\n✓ {name}"


# ── 論文PDF処理 ──────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = []
        for page in pdf.pages[:30]:  # 最大30ページ
            text = page.extract_text()
            if text:
                texts.append(text)
    return "\n".join(texts)


def summarize_paper(text: str) -> dict:
    claude = get_claude()
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": f"""以下の論文テキストを解析し、JSONのみ返してください。

{{
  "title": "論文タイトル",
  "authors": "著者名（カンマ区切り）",
  "background": "背景・研究動機（200字以内）",
  "method": "手法（200字以内）",
  "results": "結果（200字以内）",
  "discussion": "考察・結論（200字以内）"
}}

論文テキスト:
{text[:8000]}""",
            }
        ],
    )
    text_out = response.content[0].text.strip()
    if "```" in text_out:
        text_out = text_out.split("```")[1]
        if text_out.startswith("json"):
            text_out = text_out[4:]
    return json.loads(text_out.strip())


def save_paper_to_notion(summary: dict, filename: str) -> str:
    notion = get_notion()
    today = now_jst().strftime("%Y-%m-%d")

    page = notion.pages.create(
        parent={"database_id": NOTION_PAPERS_DB},
        properties={
            "Name": {"title": [{"text": {"content": summary.get("title", filename)}}]},
            "Authors": {"rich_text": [{"text": {"content": summary.get("authors", "")}}]},
            "Background": {"rich_text": [{"text": {"content": summary.get("background", "")}}]},
            "Method": {"rich_text": [{"text": {"content": summary.get("method", "")}}]},
            "Results": {"rich_text": [{"text": {"content": summary.get("results", "")}}]},
            "Discussion": {"rich_text": [{"text": {"content": summary.get("discussion", "")}}]},
            "My Notes": {"rich_text": [{"text": {"content": ""}}]},
            "Added": {"date": {"start": today}},
        },
    )
    return page["url"]


def process_pdf(message_id: str, filename: str) -> str:
    line_bot_api = get_line_bot_api()
    content = line_bot_api.get_message_content(message_id)
    pdf_bytes = b"".join(chunk for chunk in content.iter_content())

    text = extract_pdf_text(pdf_bytes)
    if not text.strip():
        return "PDFからテキストを抽出できませんでした。スキャン画像のみのPDFは現在非対応です。"

    summary = summarize_paper(text)
    page_url = save_paper_to_notion(summary, filename)

    return (
        f"論文を保存しました！\n\n"
        f"📄 {summary.get('title', filename)}\n"
        f"👤 {summary.get('authors', '')}\n\n"
        f"🔗 Notionで確認:\n{page_url}"
    )


# ── メッセージ処理 ────────────────────────────────────────

def process_message(user_message: str) -> str:
    try:
        info = parse_with_ai(user_message)
        action = info.get("action")

        if action == "add_event" and info.get("title") and info.get("date") and info.get("start_time"):
            return add_event(info)
        elif action == "list_events":
            date = info.get("date") or now_jst().strftime("%Y-%m-%d")
            return list_events(date)
        elif action == "delete_event":
            return delete_event(info)
        elif action == "add_task" and info.get("title"):
            return add_task(info)
        elif action == "list_tasks":
            return list_tasks()
        elif action == "complete_task":
            return complete_task(info)
        else:
            return (
                "こんな感じで送ってください！\n\n"
                "【予定】\n"
                "登録：「明日14時 田中さんとMTG」\n"
                "確認：「今日の予定は？」\n"
                "削除：「明日のMTGを削除して」\n\n"
                "【タスク】\n"
                "追加：「資料作成をタスクに追加、期限4/5」\n"
                "確認：「タスク一覧」\n"
                "完了：「資料作成完了」\n\n"
                "【論文】\n"
                "PDFファイルを送信するだけ！"
            )
    except Exception as e:
        return f"エラーが発生しました\n{str(e)}"


# ── Webhook handler ───────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        signature = self.headers.get("X-Line-Signature", "")

        line_handler = get_line_handler()
        line_bot_api = get_line_bot_api()

        try:
            @line_handler.add(MessageEvent, message=TextMessage)
            def handle_text(event):
                reply = process_message(event.message.text)
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=reply)
                )

            @line_handler.add(MessageEvent, message=FileMessage)
            def handle_file(event):
                filename = event.message.file_name or "paper.pdf"
                if not filename.lower().endswith(".pdf"):
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="PDFファイルのみ対応しています。"),
                    )
                    return
                reply = process_pdf(event.message.id, filename)
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text=reply)
                )

            line_handler.handle(body, signature)
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
