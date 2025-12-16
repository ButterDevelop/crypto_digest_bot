# db.py
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Dict, Any

from config import settings


@dataclass
class User:
    user_id: int
    is_admin: bool
    balance_stars: int
    subscription_until: Optional[datetime]
    free_digest_used: bool
    last_renewal_reminder_date: Optional[date]
    language: str
    delivery_mode: str  # 'pdf' | 'messages'
    created_at: datetime
    updated_at: datetime

@dataclass
class Report:
    id: int
    kind: str  # 'daily' | 'weekly' | 'monthly'
    period_start_utc: datetime
    period_end_utc: datetime
    json_content: str
    html_content: Optional[str]
    pdf_path: Optional[str]
    is_sent: bool
    created_at: datetime


@dataclass
class SupportTicket:
    id: int
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    language_code: Optional[str]
    message: str
    admin_message_id: Optional[int]  # Message ID sent to admin (for reply matching)
    admin_id: Optional[int]          # Which admin received this ticket
    response: Optional[str]          # Admin's response
    status: str                      # 'open' | 'answered'
    created_at: datetime
    responded_at: Optional[datetime]
    media_type: Optional[str]        # Type of media: 'photo', 'document', 'video', 'audio', 'voice'
    media_file_id: Optional[str]     # Telegram file_id for forwarding
    media_caption: Optional[str]     # Caption for media


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create users table if it does not exist."""
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_admin INTEGER NOT NULL DEFAULT 0,
            balance_stars INTEGER NOT NULL DEFAULT 0,
            subscription_until TEXT,
            free_digest_used INTEGER NOT NULL DEFAULT 0,
            last_renewal_reminder_date TEXT,
            language TEXT NOT NULL DEFAULT 'ru',
            delivery_mode TEXT NOT NULL DEFAULT 'pdf',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            period_start_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            json_content TEXT NOT NULL,
            html_content TEXT,
            pdf_path TEXT,
            is_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    
    # Migration: Add is_sent column if it doesn't exist
    try:
        cur.execute("SELECT is_sent FROM reports LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute("ALTER TABLE reports ADD COLUMN is_sent INTEGER NOT NULL DEFAULT 0")
        
        # Mark old reports as sent to avoid spamming
        # "Old" means period_end_utc is older than 7 days from now
        now = datetime.now(timezone.utc)
        cur.execute(
            "UPDATE reports SET is_sent = 1 WHERE period_end_utc < ?",
            ((now - timedelta(days=7)).isoformat(),)
        )
        
        # Also mark reports sent if they are daily reports from > 2 days ago (safety net)
        cur.execute(
            "UPDATE reports SET is_sent = 1 WHERE kind = 'daily' AND period_end_utc < ?",
            ((now - timedelta(days=2)).isoformat(),)
        )
        
        conn.commit()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS translations (
            original_text TEXT NOT NULL,
            lang_code TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            PRIMARY KEY (original_text, lang_code)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language_code TEXT,
            message TEXT NOT NULL,
            admin_message_id INTEGER,
            admin_id INTEGER,
            response TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            responded_at TEXT,
            media_type TEXT,
            media_file_id TEXT,
            media_caption TEXT
        )
        """
    )

    # Add media columns if missing (migration for old DBs)
    try:
        cur.execute("SELECT media_type FROM support_tickets LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute("ALTER TABLE support_tickets ADD COLUMN media_type TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN media_file_id TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN media_caption TEXT")

    conn.commit()
    conn.close()


def _row_to_user(row: sqlite3.Row) -> User:
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _parse_date(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        return date.fromisoformat(value)

    try:
        language = row["language"] or "ru"
    except (KeyError, IndexError):
        language = "ru"

    try:
        delivery_mode = row["delivery_mode"] or "pdf"
    except (KeyError, IndexError):
        delivery_mode = "pdf"

    return User(
        user_id=row["user_id"],
        is_admin=bool(row["is_admin"]),
        balance_stars=row["balance_stars"],
        subscription_until=_parse_dt(row["subscription_until"]),
        free_digest_used=bool(row["free_digest_used"]),
        last_renewal_reminder_date=_parse_date(row["last_renewal_reminder_date"]),
        language=language,
        delivery_mode=delivery_mode,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_report(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"],
        kind=row["kind"],
        period_start_utc=datetime.fromisoformat(row["period_start_utc"]),
        period_end_utc=datetime.fromisoformat(row["period_end_utc"]),
        json_content=row["json_content"],
        html_content=row["html_content"],
        pdf_path=row["pdf_path"],
        is_sent=bool(row["is_sent"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )



def get_user(user_id: int) -> Optional[User]:
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_user(row)


def get_or_create_user(user_id: int, language: str = "ru") -> User:
    """
    Get user from DB or create a new one with defaults.
    language: optional, default 'ru'. In the future, user.language_code from Telegram can be passed.
    """
    user = get_user(user_id)
    if user:
        return user

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    is_admin = 0

    if settings.initial_admin_id is not None and user_id == settings.initial_admin_id:
        is_admin = 1

    # normalize language (two letters, lower)
    lang_norm = (language or "ru").split("-")[0].lower()

    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users
                (user_id, is_admin, balance_stars, subscription_until,
                 free_digest_used, last_renewal_reminder_date, language,
                 created_at, updated_at)
            VALUES (?, ?, 0, NULL, 0, NULL, ?, ?, ?)
            """,
            (user_id, is_admin, lang_norm, now_iso, now_iso),
        )
        conn.commit()

    return User(
        user_id=user_id,
        is_admin=bool(is_admin),
        balance_stars=0,
        subscription_until=None,
        free_digest_used=False,
        last_renewal_reminder_date=None,
        language=lang_norm,
        delivery_mode="pdf",
        created_at=now,
        updated_at=now,
    )


def update_user(user: User) -> None:
    """Persist user changes to DB."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET is_admin = ?,
                balance_stars = ?,
                subscription_until = ?,
                free_digest_used = ?,
                last_renewal_reminder_date = ?,
                language = ?,
                delivery_mode = ?,
                created_at = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                1 if user.is_admin else 0,
                user.balance_stars,
                user.subscription_until.isoformat()
                if user.subscription_until
                else None,
                1 if user.free_digest_used else 0,
                user.last_renewal_reminder_date.isoformat()
                if user.last_renewal_reminder_date
                else None,
                user.language or "ru",
                user.delivery_mode or "pdf",
                user.created_at.isoformat(),
                user.updated_at.isoformat(),
                user.user_id,
            ),
        )
        conn.commit()


def get_all_users() -> List[User]:
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users")
        rows = cur.fetchall()
        return [_row_to_user(row) for row in rows]


def create_report(
    kind: str,
    period_start_utc: datetime,
    period_end_utc: datetime,
    json_content: str,
    html_content: Optional[str] = None,
    pdf_path: Optional[str] = None,
) -> Report:
    """
    Create a report (daily/weekly/monthly) and return it as a Report object.
    """
    now = datetime.now(timezone.utc)
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reports
                (kind, period_start_utc, period_end_utc,
                 json_content, html_content, pdf_path, is_sent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                period_start_utc.isoformat(),
                period_end_utc.isoformat(),
                json_content,
                period_end_utc.isoformat(),
                json_content,
                html_content,
                pdf_path,
                0,  # is_sent = False initially
                now.isoformat(),
            ),
        )
        report_id = cur.lastrowid
        conn.commit()

    return Report(
        id=report_id,
        kind=kind,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        json_content=json_content,
        html_content=html_content,
        pdf_path=pdf_path,
        is_sent=False,
        created_at=now,
    )


def get_reports_in_range(
    kind: str,
    start_utc: datetime,
    end_utc: datetime,
) -> List[Report]:
    """
    Get all reports of the specified type in the interval [start_utc, end_utc].
    """
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM reports
            WHERE kind = ?
              AND period_start_utc >= ?
              AND period_end_utc   <= ?
            ORDER BY period_start_utc
            """,
            (kind, start_utc.isoformat(), end_utc.isoformat()),
        )
        rows = cur.fetchall()
        return [_row_to_report(row) for row in rows]


def update_report_pdf_path(report_id: int, path: str) -> None:
    """Update PDF path for a given report."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reports SET pdf_path = ? WHERE id = ?",
            (path, report_id),
        )
        conn.commit()


def get_cached_translation(original_text: str, lang_code: str) -> Optional[str]:
    """Get translation from DB if exists."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT translated_text FROM translations WHERE original_text = ? AND lang_code = ?",
            (original_text, lang_code),
        )
        row = cur.fetchone()
        if row:
            return row["translated_text"]
    return None


def save_translation(original_text: str, lang_code: str, translated_text: str) -> None:
    """Save translation to DB."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO translations (original_text, lang_code, translated_text)
            VALUES (?, ?, ?)
            """,
            (original_text, lang_code, translated_text),
        )
        conn.commit()


def get_latest_report(kind: str) -> Optional[Report]:
    """Get the latest report of a given kind."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM reports
            WHERE kind = ?
            ORDER BY period_end_utc DESC
            LIMIT 1
            """,
            (kind,),
        )
        row = cur.fetchone()
        if row:
            return _row_to_report(row)
    return None


def get_user_stats() -> Dict[str, Any]:
    """
    Get statistics about users:
    - total count
    - total stars balance
    - language distribution
    """
    stats = {
        "total_users": 0,
        "total_stars": 0,
        "languages": {}
    }
    
    with _get_connection() as conn:
        cur = conn.cursor()
        
        # Total users and stars
        cur.execute("SELECT COUNT(*), SUM(balance_stars) FROM users")
        row = cur.fetchone()
        if row:
            stats["total_users"] = row[0] or 0
            stats["total_stars"] = row[1] or 0
            
        # Language distribution
        cur.execute("SELECT language, COUNT(*) FROM users GROUP BY language")
        rows = cur.fetchall()
        for row in rows:
            lang = row[0] or "ru"
            count = row[1]
            stats["languages"][lang] = count
            
    return stats


def get_all_admin_users() -> List[User]:
    """Get all users with admin privileges."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE is_admin = 1")
        rows = cur.fetchall()
        return [_row_to_user(row) for row in rows]


def _row_to_support_ticket(row: sqlite3.Row) -> SupportTicket:
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    # Handle missing media fields in old records
    try:
        media_type = row["media_type"]
    except (KeyError, IndexError):
        media_type = None
    
    try:
        media_file_id = row["media_file_id"]
    except (KeyError, IndexError):
        media_file_id = None
    
    try:
        media_caption = row["media_caption"]
    except (KeyError, IndexError):
        media_caption = None

    return SupportTicket(
        id=row["id"],
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        language_code=row["language_code"],
        message=row["message"],
        admin_message_id=row["admin_message_id"],
        admin_id=row["admin_id"],
        response=row["response"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        responded_at=_parse_dt(row["responded_at"]),
        media_type=media_type,
        media_file_id=media_file_id,
        media_caption=media_caption,
    )


def create_support_ticket(
    user_id: int,
    message: str,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
    admin_message_id: Optional[int] = None,
    admin_id: Optional[int] = None,
    media_type: Optional[str] = None,
    media_file_id: Optional[str] = None,
    media_caption: Optional[str] = None,
) -> SupportTicket:
    """Create a new support ticket."""
    now = datetime.now(timezone.utc)
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO support_tickets
                (user_id, username, first_name, last_name, language_code,
                 message, admin_message_id, admin_id, status, created_at,
                 media_type, media_file_id, media_caption)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                first_name,
                last_name,
                language_code,
                message,
                admin_message_id,
                admin_id,
                now.isoformat(),
                media_type,
                media_file_id,
                media_caption,
            ),
        )
        ticket_id = cur.lastrowid
        conn.commit()

    return SupportTicket(
        id=ticket_id,
        user_id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        language_code=language_code,
        message=message,
        admin_message_id=admin_message_id,
        admin_id=admin_id,
        response=None,
        status="open",
        created_at=now,
        responded_at=None,
        media_type=media_type,
        media_file_id=media_file_id,
        media_caption=media_caption,
    )


def get_support_ticket_by_admin_message(admin_id: int, message_id: int) -> Optional[SupportTicket]:
    """Find a support ticket by admin's message ID."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM support_tickets WHERE admin_id = ? AND admin_message_id = ?",
            (admin_id, message_id),
        )
        row = cur.fetchone()
        if row:
            return _row_to_support_ticket(row)
    return None


def update_support_ticket_response(ticket_id: int, response: str) -> None:
    """Mark a support ticket as answered with admin's response."""
    now = datetime.now(timezone.utc)
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE support_tickets
            SET response = ?, status = 'answered', responded_at = ?
            WHERE id = ?
            """,
            (response, now.isoformat(), ticket_id),
        )
        conn.commit()


def mark_report_as_sent(report_id: int) -> None:
    """Mark a report as successfully sent to users."""
    with _get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE reports SET is_sent = 1 WHERE id = ?",
            (report_id,),
        )
        conn.commit()
