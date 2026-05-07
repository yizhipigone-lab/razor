import requests

URL = "http://localhost:3333/mcp"

def test_mcp_post_camel():
    try:
        # 1. Start session
        headers_get = {"Accept": "text/event-stream, application/json, */*"}
        r_get = requests.get(URL, headers=headers_get)
        session_id = r_get.headers.get("mcp-session-id")
        print(f"Session ID obtained: {session_id}")
        
        # 2. Try POST with sessionId (camelCase)
        if session_id:
            # Try both camelCase and snake_case as param
            post_url = f"{URL}?sessionId={session_id}"
            data = {
                "jsonrpc": "2.0",
                "method": "mcp.list_tools", # Try a real tool call
                "params": {},
                "id": 1
            }
            headers_post = {
                "Accept": "text/event-stream, application/json",
                "Content-Type": "application/json"
            }
            r_post = requests.post(post_url, json=data, headers=headers_post)
            print(f"POST Status Code: {r_post.status_code}")
            print(f"POST Response: {r_post.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_mcp_post_camel()
