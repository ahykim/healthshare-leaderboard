# October Pushup Challenge Tracker

Flask + Supabase (Postgres and Storage) app. On first visit users pick their name (or add a new one) with an optional photo, then log pushups for any challenge date; submissions add up per day. The leaderboard shows today's count, days logged, and totals.

The Flask server talks to Postgres directly with `psycopg`, and uploads profile photos to a public Supabase Storage bucket (`avatars`). The tables have RLS enabled with no policies, so nothing is reachable through the Supabase Data API.

## Run locally

Requires Docker (for the local Supabase stack) and Node (for `npx supabase`).

```sh
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

npx supabase start        # first run pulls images; prints local URLs and keys
cp .env.example .env      # then paste the local secret key from `supabase start` into SUPABASE_KEY
python app.py             # http://localhost:8000
```

- Local Studio: http://127.0.0.1:54323
- `npx supabase db reset` rebuilds the local database from `supabase/migrations/` and `supabase/seed.sql` (fake data, local only).
- `npx supabase stop` shuts the stack down.
- `npx supabase status -o env` reprints the local keys.

## Schema changes

Add SQL with `npx supabase migration new <name>`, test with `npx supabase db reset`, then deploy:

```sh
npx supabase link --project-ref <ref>   # once
npx supabase db push
```

## Deploy on a VM

Create a `.env` next to `docker-compose.yml` on the VM with the production values (see `.env.example`), then:

```sh
docker compose up -d --build
```

Edit `docker-compose.yml` to set `TZ` (determines what "today" means) and the challenge dates.

## Importing data from the old SQLite version

Copy `pushups.db` and the `uploads/` folder off the old VM (they live in the `pushup-data` volume at `/data`), then:

```sh
python scripts/import_sqlite.py --sqlite pushups.db --uploads uploads --dry-run   # validate first
python scripts/import_sqlite.py --sqlite pushups.db --uploads uploads
```

It uses the same `.env` variables as the app, so run it against local first and then against production. It verifies row counts and per-person totals before committing. Use `--replace` to wipe the target `users`/`pushups` tables and re-import. Old `created_at` values are interpreted in `America/New_York` unless you pass `--tz`.

## Configuration

| Variable | Default |
|---|---|
| `DATABASE_URL` | required. Postgres connection string (use the Transaction pooler for hosted Supabase) |
| `SUPABASE_URL` | required. Also used to build public photo URLs, so it must be reachable from browsers |
| `SUPABASE_KEY` | required. Secret / `service_role` key, server-side only |
| `AVATAR_BUCKET` | `avatars` |
| `CHALLENGE_START` | `2026-10-01` |
| `CHALLENGE_END` | `2026-10-31` |
| `PORT` | `8000` (local `python app.py` only) |

There is no authentication; anyone who can reach the app can log entries for any name.
