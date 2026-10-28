import asyncio
from app.core.database import connect_to_mongo, get_database

async def main():
    await connect_to_mongo()
    db = await get_database()
    users = await db['users'].find({'email': 'ai.developer0411@gmail.com'}).to_list(length=None)
    print("Users:", len(users))
    for u in users:
        print(u)
    otps = await db['otps'].find({'email': 'ai.developer0411@gmail.com'}).to_list(length=None)
    print("OTPs:", len(otps))
    for o in otps:
        print(o)

asyncio.run(main())
