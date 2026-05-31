import asyncio
import re
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.json")


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. Follow Google Calendar setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


def _parse_date_sync(deadline_str: str, model) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}.\n"
        f'Convert this deadline to ISO date format (YYYY-MM-DD): "{deadline_str}"\n'
        "Return ONLY the date string, nothing else. Example: 2024-06-05"
    )
    from gemini_service import _with_retry
    response = _with_retry(model.generate_content, prompt)
    text = response.text.strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group()
    raise ValueError(f"Cannot parse date from: {text}")


async def parse_deadline_to_date(deadline_str: str, model) -> str:
    return await asyncio.to_thread(_parse_date_sync, deadline_str, model)


def _create_calendar_event_sync(service, task: dict, date_str: str) -> str:
    description = task.get("description") or ""
    if task.get("responsible"):
        description += f"\nВідповідальний: {task['responsible']}"

    event = {
        "summary": task["title"],
        "description": description.strip(),
        "start": {"date": date_str},
        "end": {"date": date_str},
    }
    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")


def _create_google_task_sync(service, task: dict, date_str: str) -> None:
    notes = task.get("description") or ""
    if task.get("responsible"):
        notes += f"\nВідповідальний: {task['responsible']}"

    body = {
        "title": task["title"],
        "notes": notes.strip(),
        "due": f"{date_str}T00:00:00.000Z",
    }
    service.tasks().insert(tasklist="@default", body=body).execute()


def _add_to_calendar_sync(tasks_with_dates: list[dict]) -> list[str]:
    creds = get_credentials()
    cal_service = build("calendar", "v3", credentials=creds)
    tasks_service = build("tasks", "v1", credentials=creds)

    links = []
    for item in tasks_with_dates:
        task = item["task"]
        date_str = item["date"]
        link = _create_calendar_event_sync(cal_service, task, date_str)
        _create_google_task_sync(tasks_service, task, date_str)
        links.append(link)
    return links


async def add_to_calendar(tasks_with_dates: list[dict]) -> list[str]:
    return await asyncio.to_thread(_add_to_calendar_sync, tasks_with_dates)
