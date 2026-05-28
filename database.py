import sqlite3
from pathlib import Path

DB_PATH = Path("tasks.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER,
                transcript TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voice_message_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                responsible TEXT,
                deadline TEXT,
                priority TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (voice_message_id) REFERENCES voice_messages(id)
            )
        """)
        conn.commit()


def save_voice_message(telegram_message_id: int, transcript: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO voice_messages (telegram_message_id, transcript) VALUES (?, ?)",
            (telegram_message_id, transcript),
        )
        conn.commit()
        return cursor.lastrowid


def save_tasks(voice_message_id: int, tasks: list[dict]) -> list[int]:
    ids = []
    with sqlite3.connect(DB_PATH) as conn:
        for task in tasks:
            cursor = conn.execute(
                """INSERT INTO tasks
                   (voice_message_id, title, description, responsible, deadline, priority)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    voice_message_id,
                    task.get("title", ""),
                    task.get("description"),
                    task.get("responsible"),
                    task.get("deadline"),
                    task.get("priority", "medium"),
                ),
            )
            ids.append(cursor.lastrowid)
        conn.commit()
    return ids


def get_all_tasks() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
