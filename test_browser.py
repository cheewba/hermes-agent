import os
from patchright.sync_api import sync_playwright

def run_browser():
    # Настройка прокси
    proxy = {
        "server": "http://213.109.175.82:41178",
        "username": "CPCNKEFT",
        "password": "2TIFMZ9E"
    }

    with sync_playwright() as p:
        # Запуск браузера (headless=False + xvfb в системе обеспечит headful режим)
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox"],
            proxy=proxy
        )
        
        context = browser.new_context()
        page = context.new_page()
        
        print("Загрузка страницы...")
        response = page.goto("https://google.com", wait_until="networkidle")
        
        print(f"URL: {page.url}")
        print(f"Status: {response.status}")
        
        # Проверка, что мы на гугле
        print("Title:", page.title())
        
        browser.close()

if __name__ == "__main__":
    # Убедимся, что xvfb запущен, если мы в headful окружении
    # Обычно это делается через 'xvfb-run python script.py'
    run_browser()
