
from patchright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.zenrows.com/blog/patchright", timeout=30000)
    page.wait_for_selector("h1, h2, p", timeout=10000)
    
    with open("/opt/hermes-agent/worktrees/dev-develop/zenrows_full.txt", "w") as f:
        elements = page.locator("p, h2, h3, pre").all()
        for el in elements:
            f.write(el.inner_text().strip() + "\n")
            
    browser.close()
