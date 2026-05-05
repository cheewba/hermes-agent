
from patchright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.zenrows.com/blog/patchright", timeout=30000)
    page.wait_for_timeout(2000) # wait a bit
    
    with open("/opt/hermes-agent/worktrees/dev-develop/zenrows_html.txt", "w") as f:
        f.write(page.content())
            
    browser.close()
