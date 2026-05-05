
from patchright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://www.zenrows.com/blog/patchright", timeout=30000)
        print("TITLE:", page.title())
        with open("/opt/hermes-agent/worktrees/dev-develop/zenrows_title.txt", "w") as f:
            f.write(page.title() + "\n" + page.content()[:1000])
        browser.close()
except Exception as e:
    print("ERR:", e)
