import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8686"))
    reload_enabled = os.environ.get("RELOAD", "0").lower() in ("1", "true", "yes")
    uvicorn.run(
        "app.server:app",
        host=host,
        port=port,
        reload=reload_enabled,
        timeout_graceful_shutdown=2,
    )



