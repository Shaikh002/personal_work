import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="ig_user_data",
            headless=False,
            args=["--window-position=100,100", "--window-size=1280,720"]
        )
        page = await browser.new_page()
        await page.goto("https://www.instagram.com")
        print("👉 Please log in manually.")
        input("✅ Press Enter after login...")  # waits until user logs in
        await browser.close()

asyncio.run(main())
