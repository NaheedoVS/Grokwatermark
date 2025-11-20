import motor.motor_asyncio
from configs import DATABASE_URL

db_client = motor.motor_asyncio.AsyncIOMotorClient(DATABASE_URL)
db = db_client.watermark_bot

async def get_user_data(user_id: int):
    data = await db.users.find_one({"_id": user_id})
    return data or {}

async def set_user_data(user_id: int, data: dict):
    await db.users.update_one({"_id": user_id}, {"$set": data}, upsert=True)

async def get_text_data(user_id: int):
    data = await db.users.find_one({"_id": user_id})
    return data.get("text_settings", {"text": "", "color": "white", "size": 24, "use": False})

async def set_text_data(user_id: int, data: dict):
    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"text_settings": data}},
        upsert=True
    )
