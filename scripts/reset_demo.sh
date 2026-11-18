#!/usr/bin/env bash
# Rebuild the DEMO database from scratch with synthetic data. Refuses to touch anything but a database named *_demo.
set -euo pipefail
cd "$(dirname "$0")/.."
URL=$(grep ^DATABASE_URL .env | cut -d= -f2-)
DEMO_URL=$(echo "$URL" | sed -E 's#/[A-Za-z0-9_]+$#/cosmetics_demo#')
python - "$DEMO_URL" <<'PY'
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
u = make_url(sys.argv[1]); assert u.database.endswith("_demo"), "refusing: not a demo database"
e = create_engine(u.set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
with e.connect() as c:
    c.execute(text(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)'))
    c.execute(text(f'CREATE DATABASE "{u.database}"'))
PY
export DATABASE_URL="$DEMO_URL" ENVIRONMENT=demo
python -m alembic upgrade head >/dev/null
python -m backend.cli init
python -m backend.cli seed-demo
python -m backend.cli seed-demo-catalogue
