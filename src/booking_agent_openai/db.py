"""予約データの層。SQLite に店舗の予約を保存する（本番の予約システムの代わりのモック）。"""

import sqlite3
from datetime import date, datetime, timedelta

SHOP = {
    "name": "ヘアサロン・ハナ",
    "address": "東京都渋谷区（架空の店舗）",
    "hours": "10:00〜18:00（最終受付 17:00）",
    "closed": "毎週火曜日",
    "phone": "03-0000-0000",
}

# menu_id: (名前, 所要時間(分), 料金(円))
MENUS = {
    "cut": ("カット", 60, 4400),
    "color": ("カラー", 120, 7700),
    "cut_color": ("カット＋カラー", 180, 11000),
    "headspa": ("ヘッドスパ", 60, 3300),
}

OPEN_HOUR, CLOSE_HOUR = 10, 18
CLOSED_WEEKDAY = 1  # 月曜=0 なので火曜=1


def connect(path: str = "booking.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            menu_id TEXT NOT NULL,
            start TEXT NOT NULL,       -- 'YYYY-MM-DD HH:MM'
            end TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reason TEXT NOT NULL,
            summary TEXT NOT NULL
        )"""
    )
    return conn


def _parse(day: str, time: str) -> datetime:
    return datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")


def _overlaps(conn, start: datetime, end: datetime) -> bool:
    rows = conn.execute("SELECT start, end FROM bookings WHERE status = 'confirmed'").fetchall()
    for r in rows:
        s = datetime.strptime(r["start"], "%Y-%m-%d %H:%M")
        e = datetime.strptime(r["end"], "%Y-%m-%d %H:%M")
        if start < e and s < end:
            return True
    return False


def available_times(conn, day: str, menu_id: str) -> list[str]:
    """その日にそのメニューで予約できる開始時刻（1時間刻み）を返す。"""
    if menu_id not in MENUS:
        raise ValueError(f"unknown menu_id: {menu_id}")
    d = date.fromisoformat(day)
    if d.weekday() == CLOSED_WEEKDAY:
        return []
    minutes = MENUS[menu_id][1]
    times = []
    for hour in range(OPEN_HOUR, CLOSE_HOUR):
        start = datetime(d.year, d.month, d.day, hour)
        end = start + timedelta(minutes=minutes)
        if end > datetime(d.year, d.month, d.day, CLOSE_HOUR):
            break
        if start <= datetime.now():
            continue
        if not _overlaps(conn, start, end):
            times.append(start.strftime("%H:%M"))
    return times


def create_booking(conn, customer_name: str, phone: str, menu_id: str, day: str, start_time: str) -> dict:
    if start_time not in available_times(conn, day, menu_id):
        raise ValueError(f"{day} {start_time} は {MENUS[menu_id][0]} で予約できません（空いていない、定休日、または営業時間外）")
    start = _parse(day, start_time)
    end = start + timedelta(minutes=MENUS[menu_id][1])
    cur = conn.execute(
        "INSERT INTO bookings (customer_name, phone, menu_id, start, end) VALUES (?, ?, ?, ?, ?)",
        (customer_name, phone, menu_id, start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    return {"booking_id": cur.lastrowid, "menu": MENUS[menu_id][0], "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%H:%M"), "price_yen": MENUS[menu_id][2]}


def find_bookings(conn, phone: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, customer_name, menu_id, start, end FROM bookings WHERE phone = ? AND status = 'confirmed' ORDER BY start",
        (phone,),
    ).fetchall()
    return [{"booking_id": r["id"], "name": r["customer_name"], "menu": MENUS[r["menu_id"]][0],
             "start": r["start"], "end": r["end"]} for r in rows]


def cancel_booking(conn, booking_id: int, phone: str) -> dict:
    row = conn.execute(
        "SELECT id FROM bookings WHERE id = ? AND phone = ? AND status = 'confirmed'", (booking_id, phone)
    ).fetchone()
    if row is None:
        raise ValueError("その予約番号と電話番号の組み合わせの予約は見つかりません")
    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    conn.commit()
    return {"booking_id": booking_id, "status": "cancelled"}


def log_handoff(conn, reason: str, summary: str) -> dict:
    conn.execute("INSERT INTO handoffs (reason, summary) VALUES (?, ?)", (reason, summary))
    conn.commit()
    return {"status": "店主に引き継ぎました。店主から折り返し連絡します。"}
