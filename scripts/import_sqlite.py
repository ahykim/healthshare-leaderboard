"""Import an existing pushups.db (and its uploads/ folder) into Supabase.

Usage:
    python scripts/import_sqlite.py --sqlite path/to/pushups.db --uploads path/to/uploads [--dry-run] [--replace]

Reads POSTGRES_URL, SUPABASE_URL, SUPABASE_SECRET_KEY (and optionally AVATAR_BUCKET) from the
environment or .env, so point those at local first, then at production.

The import runs in a single transaction and checks row counts and per-person totals against
the SQLite source before committing. Photos are uploaded first (upsert, so re-runs are safe).
By default it refuses to run if the target already has pushups; --replace empties the target
`pushups` and `users` tables first.
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from supabase import create_client


def die(msg):
    sys.exit(f"error: {msg}")


def clean_postgres_url(url):
    """The Vercel/Supabase integration can append a `supa=...` query param that libpq rejects."""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "supa"])
    return urlunsplit(parts._replace(query=query))


def read_source(path):
    if not os.path.isfile(path):
        die(f"SQLite file not found: {path}")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tables = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    if "pushups" in tables:
        rows = [tuple(r) for r in src.execute("SELECT name, day, count, created_at FROM pushups")]
    elif "entries" in tables:  # older one-row-per-day schema, never upgraded by the app
        rows = [tuple(r) for r in src.execute("SELECT name, day, count, updated_at FROM entries WHERE count > 0")]
    else:
        die("no pushups (or legacy entries) table found in that database")

    users = {}  # lower-case name -> (canonical name, photo)
    if "users" in tables:
        for r in src.execute("SELECT name, photo FROM users"):
            users[r["name"].lower()] = (r["name"], r["photo"])
    for name, *_ in rows:  # names logged before profiles existed
        users.setdefault(name.lower(), (name, None))
    return users, rows


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True, help="path to pushups.db copied from the VM")
    ap.add_argument("--uploads", help="path to the uploads/ folder copied from the VM")
    ap.add_argument("--tz", default="America/New_York", help="timezone the SQLite created_at values were written in")
    ap.add_argument("--dry-run", action="store_true", help="validate and report, then roll back (no photo upload)")
    ap.add_argument("--replace", action="store_true", help="empty target users/pushups before importing")
    args = ap.parse_args()

    db_url = clean_postgres_url(os.environ.get("POSTGRES_URL") or die("POSTGRES_URL is not set"))
    sb_url = (os.environ.get("SUPABASE_URL") or die("SUPABASE_URL is not set")).rstrip("/")
    sb_key = os.environ.get("SUPABASE_SECRET_KEY") or die("SUPABASE_SECRET_KEY is not set")
    bucket = os.environ.get("AVATAR_BUCKET", "avatars")
    tz = ZoneInfo(args.tz)

    users, rows = read_source(args.sqlite)
    print(f"Source: {len(users)} users, {len(rows)} pushup rows, {sum(r[2] for r in rows)} total pushups")
    print(f"Target: {db_url.split('@')[-1]}  |  {sb_url}")

    # Photos: make sure each referenced file exists, else import the user without one.
    photos = {}
    for key, (name, photo) in users.items():
        if photo and args.uploads and os.path.isfile(os.path.join(args.uploads, photo)):
            photos[key] = photo
        elif photo:
            print(f"  warning: photo for {name!r} not found ({photo}); importing without a photo")

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        existing = conn.execute("SELECT count(*) FROM public.pushups").fetchone()[0]
        if existing and not args.replace:
            die(f"target already has {existing} pushup rows; use --replace to empty users/pushups first")

        if photos and not args.dry_run:
            storage = create_client(sb_url, sb_key).storage.from_(bucket)
            for key, filename in photos.items():
                with open(os.path.join(args.uploads, filename), "rb") as f:
                    storage.upload(
                        filename,
                        f.read(),
                        {"content-type": "image/jpeg", "cache-control": str(60 * 60 * 24 * 365), "upsert": "true"},
                    )
            print(f"Uploaded {len(photos)} photos to bucket {bucket!r}")

        if args.replace:
            conn.execute("TRUNCATE public.pushups, public.users")

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.users (name, photo) VALUES (%s, %s)",
                [(name, photos.get(key)) for key, (name, _) in users.items()],
            )
            cur.executemany(
                "INSERT INTO public.pushups (name, day, count, created_at) VALUES (%s, %s, %s, %s)",
                [
                    (
                        users[name.lower()][0],
                        date.fromisoformat(day),
                        count,
                        datetime.fromisoformat(created_at).replace(tzinfo=tz),
                    )
                    for name, day, count, created_at in rows
                ],
            )

        # Verify against the source before committing.
        n_users, n_rows, total = conn.execute(
            "SELECT (SELECT count(*) FROM public.users), count(*), COALESCE(SUM(count), 0) FROM public.pushups"
        ).fetchone()
        expected = {}
        for name, _, count, _ in rows:
            canon = users[name.lower()][0].lower()
            expected[canon] = expected.get(canon, 0) + count
        actual = {n.lower(): t for n, t in conn.execute("SELECT name, SUM(count) FROM public.pushups GROUP BY name")}
        problems = []
        if n_users != len(users):
            problems.append(f"users: expected {len(users)}, got {n_users}")
        if n_rows != len(rows):
            problems.append(f"rows: expected {len(rows)}, got {n_rows}")
        if actual != expected:
            problems.append("per-person totals differ")
        if problems:
            conn.rollback()
            die("verification failed, rolled back: " + "; ".join(problems))

        if args.dry_run:
            conn.rollback()
            print("Dry run OK (verified, then rolled back).")
        else:
            conn.commit()
            print(f"Imported and verified: {n_users} users, {n_rows} rows, {total} total pushups.")


if __name__ == "__main__":
    main()
