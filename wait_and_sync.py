import asyncio
import sys
from pathlib import Path

import httpx


PROJECT_DIR = Path(__file__).resolve().parent

CHECK_INTERVAL = 15
IP_INFO_URL = "https://ipinfo.io/json"


async def get_country() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(IP_INFO_URL)
            response.raise_for_status()

        return response.json()["country"].upper()

    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"Не удалось определить страну: {exc}")
        return None


async def wait_until_not_russia() -> None:
    last_country = None

    while True:
        country = await get_country()

        if country is None:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        if country != last_country:
            print(f"Текущая страна: {country}")
            last_country = country

        if country != "RU":
            return

        await asyncio.sleep(CHECK_INTERVAL)


async def run_sync() -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_DIR / "run.py"),
        "sync",
        cwd=PROJECT_DIR,
    )

    return await process.wait()


async def main() -> None:
    await wait_until_not_russia()

    exit_code = await run_sync()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())