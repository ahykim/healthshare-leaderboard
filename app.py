import atexit
import io
import os
import re
import uuid
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from PIL import Image, ImageOps
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from supabase import create_client

load_dotenv()


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name} (see README).")
    return value


def clean_postgres_url(url):
    """The Vercel/Supabase integration can append a `supa=...` query param that libpq rejects."""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "supa"])
    return urlunsplit(parts._replace(query=query))


POSTGRES_URL = clean_postgres_url(require_env("POSTGRES_URL"))
SUPABASE_URL = require_env("SUPABASE_URL").rstrip("/")
SUPABASE_SECRET_KEY = require_env("SUPABASE_SECRET_KEY")
AVATAR_BUCKET = os.environ.get("AVATAR_BUCKET", "avatars")
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

# prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements.
pool = ConnectionPool(
    POSTGRES_URL,
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    min_size=1,
    max_size=5,
    check=ConnectionPool.check_connection,
    open=False,
)
pool.open()
atexit.register(pool.close)

_supabase = None


def get_db():
    """Pooled connection; commits when the `with` block exits cleanly, else rolls back."""
    return pool.connection()


def storage():
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _supabase.storage.from_(AVATAR_BUCKET)


@app.template_global()
def photo_url(filename):
    return f"{SUPABASE_URL}/storage/v1/object/public/{AVATAR_BUCKET}/{filename}"


def leaderboard(today):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT u.name AS name,
                   u.photo AS photo,
                   COALESCE(SUM(p.count), 0) AS total,
                   COUNT(DISTINCT p.day) AS days,
                   COALESCE(SUM(CASE WHEN p.day = %s THEN p.count END), 0) AS today
            FROM users u
            LEFT JOIN pushups p ON p.name = u.name
            GROUP BY u.name
            ORDER BY total DESC, u.name
            """,
            (today,),
        ).fetchall()


def daily_counts():
    """Maps lower-cased name -> {iso day: total pushups}."""
    out = {}
    with get_db() as conn:
        for r in conn.execute("SELECT name, day, SUM(count) AS n FROM pushups GROUP BY name, day"):
            out.setdefault(r["name"].lower(), {})[r["day"].isoformat()] = r["n"]
    return out


def current_user():
    name = request.cookies.get(COOKIE)
    if not name:
        return None
    with get_db() as conn:
        return conn.execute("SELECT name, photo FROM users WHERE name = %s", (name,)).fetchone()


def save_photo(file_storage):
    """Validate, square-crop and downsize an upload; returns the stored filename."""
    try:
        img = ImageOps.exif_transpose(Image.open(file_storage.stream))
        img = ImageOps.fit(img.convert("RGB"), (PHOTO_SIZE, PHOTO_SIZE))
    except (OSError, Image.DecompressionBombError):
        raise ValueError("That file isn't a supported image.")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    filename = f"{uuid.uuid4().hex}.jpg"
    try:
        storage().upload(
            filename,
            buf.getvalue(),
            {"content-type": "image/jpeg", "cache-control": str(60 * 60 * 24 * 365)},
        )
    except Exception:
        app.logger.exception("Photo upload failed")
        raise ValueError("Couldn't save that photo. Please try again.")
    return filename


def delete_photo(filename):
    if filename:
        try:
            storage().remove([filename])
        except Exception:
            app.logger.exception("Photo delete failed")


def render_index(me, error=None, notice=None, form=None):
    today = date.today()
    rows = leaderboard(today)
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
            "photo": photo_url(r["photo"]) if r["photo"] else None,
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
        when = "today" if day == date.today() else (f"for {day.strftime('%b')} {day.day}" if day else "")
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
        existing = conn.execute("SELECT name, photo FROM users WHERE name = %s", (name,)).fetchone()
    if choice and not existing:
        return render_index(None, error="That name isn't on the list."), 400

    new_photo = None
    if photo and photo.filename:
        try:
            new_photo = save_photo(photo)
        except ValueError as e:
            return render_index(None, error=str(e)), 400

    old_photo = None
    try:
        with get_db() as conn:
            if existing:
                name = existing["name"]
                if new_photo:
                    conn.execute("UPDATE users SET photo = %s WHERE name = %s", (new_photo, name))
                    old_photo = existing["photo"]
            else:
                # Two people may race to claim the same new name; the loser just joins as that user.
                created = conn.execute(
                    "INSERT INTO users (name, photo) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, new_photo),
                ).rowcount
                if not created and new_photo:
                    delete_photo(new_photo)
    except Exception:
        delete_photo(new_photo)
        raise
    delete_photo(old_photo)

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
            "INSERT INTO pushups (name, day, count) VALUES (%s, %s, %s)",
            (me["name"], day, int(count_raw)),
        )
    return redirect(url_for("index", added=int(count_raw), day=day.isoformat()))


@app.errorhandler(413)
def too_large(_):
    return render_index(current_user(), error=f"Photo is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."), 413


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
