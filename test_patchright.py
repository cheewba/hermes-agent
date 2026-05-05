import os
from patchright.sync_api import sync_playwright

def test_browser():
    with sync_playwright() as p:
        # Launch browser with explicit proxy and headless=False
        # Note: Since we are in a headless Linux environment, we assume xvfb is running or we use xvfb-run
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--proxy-server=http://geo.iproyal.com:12321",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = browser.new_context(
            proxy={
                "server": "http://213.109.175.82:41178",
                "username": "CPCNKEFT",
                "password": "2TIFMZ9E"
            }
        )
        
        # Inject stealth
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        print("Navigating to https://intel.arkm.com/")
        page.goto("https://intel.arkm.com/", wait_until="networkidle", timeout=60000)
        
        print("Page title:", page.title())
        page.screenshot(path="debug_screenshot.png")
        print("Screenshot saved to debug_screenshot.png")
        
        browser.close()

if __name__ == "__main__":
    # Ensure DISPLAY is set for headful mode
    os.environ["DISPLAY"] = ":99"
    test_browser()
