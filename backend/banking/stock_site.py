"""Independent public market website. Does not initialize any bank or user database."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT
from .securities_broker import register_broker
from .stocks import register_stocks


def create_stock_app(cache_path: Path | None = None, *, broker_path: Path | None = None, key_path: Path | None = None, bank_url: str = "http://127.0.0.1:8000"):
    cache_path = cache_path or ROOT / "data" / "stock-market-cache.sqlite3"
    application = FastAPI(title="有数证券 · 独立行情服务")
    from .bank import BankError
    @application.exception_handler(BankError)
    async def business_error(request,exc):return JSONResponse({'detail':str(exc)},status_code=400)
    @application.get('/api/securities/trading-status')
    def trading_status():
        from .stock_trading import status
        return status()
    register_stocks(application, cache_path)
    register_broker(application, broker_path or cache_path.with_name("securities-users.sqlite3"), key_path or ROOT / "data" / ".securities-bridge-key", bank_url)

    @application.get("/api/health")
    def health():
        return {"status": "ok", "service": "securities"}

    @application.get("/")
    def home():
        return FileResponse(ROOT / "frontend" / "dist" / "securities.html")

    assets = ROOT / "frontend" / "dist" / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")
    return application


app = create_stock_app()
