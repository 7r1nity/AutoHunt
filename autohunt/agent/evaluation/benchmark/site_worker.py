"""Launch the bundled source-only fixture on an isolated DB and loopback port."""
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
    root=Path(__file__).resolve().parents[2]
    if (root/"evaluation/results").resolve() not in database.parents or database.exists() or args.port in {3000,8000,8080,9223}:
        raise SystemExit("Refusing non-isolated database or service port")
    if Path(args.site_root).resolve() != (root/"evaluation/fixtures/job_site").resolve():
        raise SystemExit("Only the bundled test fixture is supported")
    os.environ["DATABASE_URL"]="sqlite+aiosqlite:///"+database.as_posix()
    sys.path.insert(0,str(Path(args.site_root).resolve()))
    import app.main as site
    import uvicorn
    uvicorn.run(site.app,host="127.0.0.1",port=args.port,log_level="warning",access_log=False)
