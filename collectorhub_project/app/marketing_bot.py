import asyncio
from datetime import datetime
from sqlalchemy import select
from telethon import TelegramClient, events
from app.config import get_settings
from app.db import AsyncSessionLocal, init_db
from app.models import CollectedMessage
from app.services.filtering import check_message
from app.services.repository import get_enabled_exclusions, get_enabled_keywords, save_message
from app.services.ai import improve_text_if_enabled

settings = get_settings()


async def queue_sender(client: TelegramClient):
    while True:
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(CollectedMessage)
                .where(CollectedMessage.status == "queued")
                .order_by(CollectedMessage.created_at.asc())
                .limit(1)
            )
            msg = rows.scalar_one_or_none()
            if msg and settings.tg_target_chat:
                try:
                    text = await improve_text_if_enabled(msg.text, None)  # type: ignore[arg-type]
                    if msg.sender_name:
                        text = f"{msg.sender_name}:\n{text}"
                    await client.send_message(settings.tg_target_chat, text)
                    msg.status = "sent"
                    msg.sent_at = datetime.utcnow()
                    print(f"[sent] message_id={msg.id}")
                except Exception as exc:  # noqa: BLE001
                    msg.status = "error"
                    msg.error = str(exc)
                    print(f"[error] message_id={msg.id}: {exc}")
                await db.commit()
        await asyncio.sleep(settings.forward_delay_seconds)


async def main():
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError("Заполни TG_API_ID и TG_API_HASH в .env")

    await init_db()
    client = TelegramClient(settings.tg_session_name, settings.tg_api_id, settings.tg_api_hash)

    @client.on(events.NewMessage(chats=settings.source_chats_list or None))
    async def handler(event):
        text = event.raw_text or ""
        if not text.strip():
            return

        sender = await event.get_sender()
        sender_name = getattr(sender, "first_name", None) or getattr(sender, "username", None) or "Unknown"
        chat = await event.get_chat()
        source_chat = getattr(chat, "title", None) or str(event.chat_id)

        async with AsyncSessionLocal() as db:
            keywords = await get_enabled_keywords(db)
            exclusions = await get_enabled_exclusions(db)
            result = check_message(text, keywords, exclusions)
            status = "queued" if result.should_forward else "skipped"
            saved = await save_message(
                db,
                source_chat=source_chat,
                source_message_id=str(event.id),
                sender_name=sender_name,
                text=text,
                matched_keyword=result.matched_keyword,
                matched_exclusion=result.matched_exclusion,
                should_forward=result.should_forward,
                status=status,
            )
            print(f"[{status}] message_id={saved.id} keyword={result.matched_keyword} exclusion={result.matched_exclusion}")

    async with client:
        asyncio.create_task(queue_sender(client))
        print("CollectorHub bot started")
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
