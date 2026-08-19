from __future__ import annotations

import os

import uvicorn
from api import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("CHATGPT2API_LOCAL_HOST", "127.0.0.1"),
        port=int(os.getenv("CHATGPT2API_LOCAL_PORT", "8021")),
        access_log=False,
        log_level="info",
    )
