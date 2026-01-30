from ..database import fetch_one, fetch_all, execute_query

class ConfigService:
    @staticmethod
    async def get(key: str, default_value: str = None) -> str:
        row = await fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default_value

    @staticmethod
    async def get_number(key: str, default_value: int = None) -> int:
        val = await ConfigService.get(key)
        return int(val) if val is not None else default_value

    @staticmethod
    async def set(key: str, value: str):
        await execute_query("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))

    @staticmethod
    async def get_all() -> dict:
        rows = await fetch_all("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}
