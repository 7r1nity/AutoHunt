"""Launch ONLY the separately installed local simulator against an isolated DB.

Runs under mock-site/.venv, not the agent interpreter. Never use default DB or
seed inventory. The parent harness owns its PID and terminates only this child.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--site-root",required=True)
    parser.add_argument("--database",required=True)
    parser.add_argument("--port",type=int,required=True)
    args=parser.parse_args()
    database=Path(args.database).resolve()
    if "evaluation" not in database.parts or "results" not in database.parts or args.port in {3000,8000,8080,9223}:
        raise SystemExit("Refusing non-isolated database or service port")
    os.environ["DATABASE_URL"]="sqlite+aiosqlite:///"+database.as_posix()
    sys.path.insert(0,str(Path(args.site_root).resolve()))
    import app.main as site
    async def no_seed(_): pass
    site.seed_if_empty=no_seed
    import uvicorn
    uvicorn.run(site.app,host="127.0.0.1",port=args.port,log_level="warning",access_log=False)
