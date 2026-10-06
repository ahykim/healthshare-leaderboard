import os
import re
import sqlite3
import uuid
from datetime import date, datetime

from flask import Flask, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "pushups.db"))
UPLOAD_DIR = os.path.join(os.path.dirname(DB_PATH), "uploads")
START = date.fromisoformat(os.environ.get("CHALLENGE_START", "2026-10-01"))
END = date.fromisoformat(os.environ.get("CHALLENGE_END", "2026-10-31"))
MAX_DAILY = 2000
MAX_NAME_LEN = 50
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
PHOTO_SIZE = 256
COOKIE = "pushup_name"
COOKIE_MAX_AGE = 60 * 60 * 24 * 180

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pushups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE,
                day TEXT NOT NULL,
                count INTEGER NOT NULL CHECK (count > 0),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                photo TEXT
            )
            """
        )
        # Carry over data from the earlier one-row-per-day schema.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entries'").fetchone():
            conn.execute(
                "INSERT INTO pushups (name, day, count, created_at) "
                "SELECT name, day, count, updated_at FROM entries WHERE count > 0"
            )
            conn.execute("DROP TABLE entries")
        # Names logged before profiles existed become selectable users.
        conn.execute("INSERT OR IGNORE INTO users (name) SELECT DISTINCT name FROM pushups")


def leaderboard(today_iso):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT u.name AS name,
                   u.photo AS photo,
                   COALESCE(SUM(p.count), 0) AS total,
                   COUNT(DISTINCT p.day) AS days,
                   COALESCE(SUM(CASE WHEN p.day = ? THEN p.count END), 0) AS today
            FROM users u
            LEFT JOIN pushups p ON p.name = u.name
            GROUP BY u.name
            ORDER BY total DESC, u.name COLLATE NOCASE
            """,
            (today_iso,),
        ).fetchall()


def daily_counts():
    """Maps lower-cased name -> {iso day: total pushups}."""
    out = {}
    with get_db() as conn:
        for r in conn.execute("SELECT name, day, SUM(count) AS n FROM pushups GROUP BY name, day"):
            out.setdefault(r["name"].lower(), {})[r["day"]] = r["n"]
    return out


def current_user():
    name = request.cookies.get(COOKIE)
    if not name:
        return None
    with get_db() as conn:
        return conn.execute("SELECT name, photo FROM users WHERE name = ?", (name,)).fetchone()


def save_photo(file_storage):
    """Validate, square-crop and downsize an upload; returns the stored filename."""
    try:
        img = ImageOps.exif_transpose(Image.open(file_storage.stream))
        img = ImageOps.fit(img.convert("RGB"), (PHOTO_SIZE, PHOTO_SIZE))
    except (OSError, Image.DecompressionBombError):
        raise ValueError("That file isn't a supported image.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.jpg"
    img.save(os.path.join(UPLOAD_DIR, filename), "JPEG", quality=85)
    return filename


def delete_photo(filename):
    if filename:
        try:
            os.remove(os.path.join(UPLOAD_DIR, filename))
        except OSError:
            pass


def render_index(me, error=None, notice=None, form=None):
    today = date.today()
    rows = leaderboard(today.isoformat())
    if me is None:
        return render_template(
            "welcome.html",
            users=sorted(rows, key=lambda r: r["name"].lower()),
            start=START,
            end=END,
            max_name_len=MAX_NAME_LEN,
            error=error,
        )
    last = min(today, END)
    daily = daily_counts()
    people = [
        {
            "name": r["name"],
            "photo": url_for("photo", filename=r["photo"]) if r["photo"] else None,
            "total": r["total"],
            "daily": daily.get(r["name"].lower(), {}),
        }
        for r in rows
    ]
    days = [date.fromordinal(o).isoformat() for o in range(START.toordinal(), last.toordinal() + 1)]
    return render_template(
        "index.html",
        me=me,
        rows=rows,
        people=people,
        days=days,
        active=today >= START,
        max_day=last,
        start=START,
        end=END,
        max_daily=MAX_DAILY,
        max_name_len=MAX_NAME_LEN,
        error=error,
        notice=notice,
        form=form or {},
    )


@app.get("/")
def index():
    notice = None
    added = request.args.get("added", type=int)
    if added:
        try:
            day = date.fromisoformat(request.args.get("day", ""))
        except ValueError:
            day = None
        when = "today" if day == date.today() else (f"for {day.strftime('%b %-d')}" if day else "")
        notice = f"Added {added} pushups {when}.".replace(" .", ".")
    return render_index(current_user(), notice=notice)


@app.post("/join")
def join():
    choice = request.form.get("existing", "").strip()
    name = choice or re.sub(r"\s+", " ", request.form.get("name", "")).strip()
    photo = request.files.get("photo")

    if not name or len(name) > MAX_NAME_LEN:
        return render_index(None, error=f"Select your name or enter a new one (up to {MAX_NAME_LEN} characters)."), 400

    with get_db() as conn:
        existing = conn.execute("SELECT name, photo FROM users WHERE name = ?", (name,)).fetchone()
        if choice and not existing:
            return render_index(None, error="That name isn't on the list."), 400

        new_photo = None
        if photo and photo.filename:
            try:
                new_photo = save_photo(photo)
            except ValueError as e:
                return render_index(None, error=str(e)), 400

        if existing:
            name = existing["name"]
            if new_photo:
                conn.execute("UPDATE users SET photo = ? WHERE name = ?", (new_photo, name))
                delete_photo(existing["photo"])
        else:
            conn.execute("INSERT INTO users (name, photo) VALUES (?, ?)", (name, new_photo))

    resp = redirect(url_for("index"))
    resp.set_cookie(COOKIE, name, max_age=COOKIE_MAX_AGE, httponly=True, samesite="Lax")
    return resp


@app.get("/switch")
def switch():
    resp = redirect(url_for("index"))
    resp.delete_cookie(COOKIE)
    return resp


@app.post("/log")
def log():
    me = current_user()
    if not me:
        return redirect(url_for("index"))

    day_raw = request.form.get("day", "")
    count_raw = request.form.get("count", "").strip()
    form = {"day": day_raw, "count": count_raw}

    try:
        day = date.fromisoformat(day_raw)
    except ValueError:
        return render_index(me, error="Pick a valid date.", form=form), 400
    if not (START <= day <= min(END, date.today())):
        return render_index(me, error="Date must be a past or current day within the challenge.", form=form), 400
    if not count_raw.isdigit() or not (1 <= int(count_raw) <= MAX_DAILY):
        return render_index(me, error=f"Count must be a whole number from 1 to {MAX_DAILY}.", form=form), 400

    with get_db() as conn:
        conn.execute(
            "INSERT INTO pushups (name, day, count, created_at) VALUES (?, ?, ?, ?)",
            (me["name"], day.isoformat(), int(count_raw), datetime.now().isoformat(timespec="seconds")),
        )
    return redirect(url_for("index", added=int(count_raw), day=day.isoformat()))


@app.get("/photos/<filename>")
def photo(filename):
    return send_from_directory(UPLOAD_DIR, filename, max_age=60 * 60 * 24 * 365)


@app.errorhandler(413)
def too_large(_):
    return render_index(current_user(), error=f"Photo is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."), 413


@app.get("/healthz")
def healthz():
    return "ok"


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
