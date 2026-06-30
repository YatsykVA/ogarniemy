from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Keyword, Exclusion, CollectedMessage


async def get_enabled_keywords(db: AsyncSession) -> list[str]:
    rows = await db.execute(select(Keyword).where(Keyword.enabled == True))  # noqa: E712
    return [x.phrase for x in rows.scalars().all()]


async def get_enabled_exclusions(db: AsyncSession) -> list[str]:
    rows = await db.execute(select(Exclusion).where(Exclusion.enabled == True))  # noqa: E712
    return [x.phrase for x in rows.scalars().all()]


async def save_message(db: AsyncSession, **kwargs) -> CollectedMessage:
    msg = CollectedMessage(**kwargs)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg
