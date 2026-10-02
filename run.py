#!/usr/bin/env python3
"""Start InternStash.

    python run.py

Honours the INTERNSTASH_* environment variables documented in app/config.py
and .env.example.
"""

import uvicorn

from app import config

if __name__ == "__main__":
    print(f"InternStash — data directory: {config.DATA_DIR}")
    print(f"                    database: {config.DB_PATH}")
    print(f"  no auth: keep this on your private network (http://{config.HOST}:{config.PORT})")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
