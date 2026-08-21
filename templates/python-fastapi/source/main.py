import logging
import contextlib
from typing import Dict, Any, List
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from source.database import engine, get_db, settings
from source.models import Entry
from source.auth import get_user_from_headers

# Structured logging setup
logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}')
handler.setFormatter(formatter)
logger.addHandler(handler)

templates = Jinja2Templates(directory="source/templates")

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up")
    yield
    logger.info("Application shutting down, disposing database engine")
    engine.dispose()

app = FastAPI(
    title="Python FastAPI App",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

class EntryCreate(BaseModel):
    content: str

@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        logger.error(f"Healthcheck failed: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, db: Session = Depends(get_db), user: Dict[str, str] = Depends(get_user_from_headers)):
    entries = db.query(Entry).all()
    return templates.TemplateResponse(request, "index.html", {"entries": entries, "user": user})

@app.post("/api/entries")
def create_entry(entry: EntryCreate, db: Session = Depends(get_db), user: Dict[str, str] = Depends(get_user_from_headers)):
    author = user.get("username") or "Anonymous"
    db_entry = Entry(content=entry.content, author=author)
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return {"id": db_entry.id, "content": db_entry.content, "author": db_entry.author}

@app.get("/api/entries")
def read_entries(db: Session = Depends(get_db)):
    entries = db.query(Entry).all()
    return [{"id": e.id, "content": e.content, "author": e.author} for e in entries]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("source.main:app", host=settings.host, port=settings.port)
