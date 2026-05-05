
import json
import time
import os
from typing import Dict, Any
from tools.registry import registry
from tools.browser_backend_registry import get_browser_backend
from tools.browser_backends.patchright import _run_in_pw_thread
from hermes_constants import get_hermes_home

def _check_arkham_reqs() -> bool:
    return True

@_run_in_pw_thread
def _run_arkham_analysis(backend, action: str, target: str, task_id: str, cookies: list = None) -> Dict[str, Any]:
    arkham_task_id = "arkham_persistent_session"
    state = backend.get_session(arkham_task_id)
    if not state:
        state = backend.init_session(arkham_task_id)
        
    page = state.page
    context = page.context
    
    # Try loading cookies from environment or passed args
    if not cookies:
        cookie_path = os.path.join(get_hermes_home(), "arkham_cookies.json")
        if os.path.exists(cookie_path):
            with open(cookie_path, "r") as f:
                cookies = json.load(f)
                
    if cookies:
        formatted_cookies = []
        for c in cookies:
            if "domain" not in c:
                c["domain"] = ".arkm.com"
            if "path" not in c:
                c["path"] = "/"
            formatted_cookies.append(c)
        context.add_cookies(formatted_cookies)
    
    page.goto("https://intel.arkm.com", wait_until="domcontentloaded")
    time.sleep(5)
    
    # Handle CF
    if "Just a moment" in page.title():
        loc = page.locator("text='Solve with 2Captcha'").first
        if loc:
            loc.click()
            for _ in range(60):
                time.sleep(2)
                if "Just a moment" not in page.title():
                    break
                    
    time.sleep(5)
    
    if "login" in page.url.lower() or "Log In" in page.content():
        return {
            "success": False, 
            "error": "Not logged in to Arkham. Please provide valid session cookies.",
            "url": page.url
        }
        
    api_responses = {}
    def on_response(response):
        try:
            url = response.url
            if "api.arkm.com" in url or "intel.arkm.com" in url:
                if response.status == 200:
                    data = response.json()
                    # Capture basically anything interesting
                    if "trending" in url or "insights" in url:
                        api_responses['trending'] = data
                    elif "holders" in url.lower():
                        api_responses['holders'] = data
                    elif "transfers" in url.lower() or "txs" in url.lower() or "transactions" in url.lower():
                        api_responses['transactions'] = data
                    elif "open-interest" in url.lower() or "oi" in url.lower() or "futures" in url.lower():
                        api_responses['open_interest'] = data
                    elif target.lower() in url.lower():
                        api_responses[url] = data
        except:
            pass
            
    page.on("response", on_response)
    
    try:
        # Generic Arkham search
        search_input = page.locator('input[placeholder*="Search"], input[type="text"]').first
        search_input.fill(target)
        time.sleep(3)
        page.keyboard.press("Enter")
        time.sleep(10) # Wait for page to load and APIs to fire
    except Exception as e:
        return {"success": False, "error": f"Failed to interact with UI: {e}"}
        
    return {
        "success": True,
        "action": action,
        "target": target,
        "captured_data": api_responses
    }

def arkham_analyze(action: str, target: str, cookies: list = None, task_id: str = None) -> str:
    """
    Analyzes Arkham Intelligence data using an authenticated browser session.
    """
    backend = get_browser_backend()
    if backend.__class__.__name__ != "PatchrightBackend":
        return json.dumps({"success": False, "error": "Arkham tool requires patchright backend."})
        
    try:
        result = _run_arkham_analysis(backend, action, target, task_id, cookies)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})

registry.register(
    name="arkham_analyze",
    toolset="arkham",
    schema={
        "name": "arkham_analyze",
        "description": "Analyze Arkham Intelligence data (holders, transactions, open interest) using an authenticated browser session.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["holders", "transactions", "open_interest", "full_scan"],
                    "description": "What data to extract."
                },
                "target": {
                    "type": "string",
                    "description": "Token symbol or wallet address to analyze (e.g. 'ARKM', '0x123...')."
                },
                "cookies": {
                    "type": "array",
                    "items": {
                        "type": "object"
                    },
                    "description": "Optional list of cookie objects to authenticate."
                }
            },
            "required": ["action", "target"]
        }
    },
    handler=lambda args, **kwargs: arkham_analyze(args.get("action"), args.get("target"), args.get("cookies"), kwargs.get("task_id")),
    check_fn=_check_arkham_reqs
)
