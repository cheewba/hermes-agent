
import json
import time
from typing import Dict, Any
from tools.registry import registry
from tools.browser_backend_registry import get_browser_backend
from tools.browser_backends.patchright import _run_in_pw_thread

def _check_arkham_reqs() -> bool:
    return True

@_run_in_pw_thread
def _run_arkham_analysis(backend, action: str, target: str, task_id: str) -> Dict[str, Any]:
    # Use a persistent dedicated session ID for Arkham so login state is preserved
    arkham_task_id = "arkham_persistent_session"
    state = backend.get_session(arkham_task_id)
    if not state:
        state = backend.init_session(arkham_task_id)
        
    page = state.page
    
    # 1. Navigate
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
    
    # Check if we are on login page
    if "login" in page.url.lower():
        return {
            "success": False, 
            "error": "Not logged in to Arkham. Please run a browser session to log in manually first, or automate login.",
            "url": page.url
        }
        
    # We are logged in.
    # Set up network interceptor to catch the data
    api_responses = {}
    
    def on_response(response):
        try:
            url = response.url
            if "api.arkm.com" in url or "intel.arkm.com" in url:
                if response.status == 200:
                    # We might catch token holders, txs etc based on URL patterns
                    if "holders" in url.lower():
                        api_responses['holders'] = response.json()
                    elif "transfers" in url.lower() or "txs" in url.lower():
                        api_responses['transactions'] = response.json()
                    elif "open-interest" in url.lower() or "oi" in url.lower():
                        api_responses['open_interest'] = response.json()
        except:
            pass
            
    page.on("response", on_response)
    
    # 2. Perform the action via UI to trigger the API calls
    # Since we don't have the exact DOM selectors (due to lack of auth), we use a generic approach:
    # Go to the search bar, type the target, and click the first result.
    try:
        # Generic Arkham search
        search_input = page.locator('input[placeholder*="Search"], input[type="text"]').first
        search_input.fill(target)
        time.sleep(3)
        page.keyboard.press("Enter")
        time.sleep(8) # Wait for page to load and APIs to fire
    except Exception as e:
        return {"success": False, "error": f"Failed to interact with UI: {e}"}
        
    return {
        "success": True,
        "action": action,
        "target": target,
        "captured_data": api_responses
    }

def arkham_analyze(action: str, target: str, task_id: str = None) -> str:
    """
    Analyzes Arkham Intelligence data for a specific token or address.
    Action can be 'holders', 'transactions', 'open_interest'.
    """
    backend = get_browser_backend()
    if backend.name != "patchright":
        return json.dumps({"success": False, "error": "Arkham tool requires patchright backend."})
        
    try:
        result = _run_arkham_analysis(backend, action, target, task_id)
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
                }
            },
            "required": ["action", "target"]
        }
    },
    handler=lambda args, **kwargs: arkham_analyze(args.get("action"), args.get("target"), kwargs.get("task_id")),
    check_fn=_check_arkham_reqs
)
