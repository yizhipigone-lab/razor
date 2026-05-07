import requests

URL = "http://localhost:3333/mcp"

def test_mcp_post():
    try:
        # 1. Start session
        headers = {"Accept": "text/event-stream"}
        r_get = requests.get(URL, headers=headers)
        session_id = r_get.headers.get("mcp-session-id")
        print(f"Session ID obtained: {session_id}")
        
        # 2. Try POST with session_id
        if session_id:
            post_url = f"{URL}?session_id={session_id}"
            data = {
                "jsonrpc": "2.0",
                "method": "ping",
                "id": 1
            }
            r_post = requests.post(post_url, json=data)
            print(f"POST Status Code: {r_post.status_code}")
            print(f"POST Response: {r_post.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_mcp_post()
