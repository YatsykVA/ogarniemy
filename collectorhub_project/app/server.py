from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.db import get_db, init_db
from app.models import CollectedMessage, Exclusion, Keyword

settings = get_settings()
app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
async def startup():
    await init_db()


@app.get("/health")
async def health():
    return {"ok": True, "ai_enabled": settings.ai_enabled}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    messages = (await db.execute(select(CollectedMessage).order_by(desc(CollectedMessage.created_at)).limit(50))).scalars().all()
    keywords = (await db.execute(select(Keyword).order_by(Keyword.phrase))).scalars().all()
    exclusions = (await db.execute(select(Exclusion).order_by(Exclusion.phrase))).scalars().all()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "messages": messages,
        "keywords": keywords,
        "exclusions": exclusions,
        "settings": settings,
    })


@app.post("/keywords")
async def add_keyword(phrase: str = Form(...), db: AsyncSession = Depends(get_db)):
    db.add(Keyword(phrase=phrase.strip()))
    await db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/exclusions")
async def add_exclusion(phrase: str = Form(...), db: AsyncSession = Depends(get_db)):
    db.add(Exclusion(phrase=phrase.strip()))
    await db.commit()
    return RedirectResponse("/", status_code=303)
