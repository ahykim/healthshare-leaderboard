# October Pushup Challenge Tracker

Flask + SQLite app. On first visit users pick their name (or add a new one) with an optional photo, then log pushups for any challenge date; submissions add up per day. The leaderboard shows today's count, days logged, and totals.

## Run locally

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py   # http://localhost:8000
```

## Deploy on a VM

```sh
docker compose up -d --build
```

Edit `docker-compose.yml` to set `TZ` (determines what "today" means) and the challenge dates. Data is stored in the `pushup-data` volume at `/data/pushups.db`, with photos in `/data/uploads`.

## Configuration

| Variable | Default |
|---|---|
| `DB_PATH` | `./data/pushups.db` |
| `CHALLENGE_START` | `2026-10-01` |
| `CHALLENGE_END` | `2026-10-31` |
| `PORT` | `8000` (local `python app.py` only) |

There is no authentication; anyone who can reach the app can log entries for any name.
