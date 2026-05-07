import requests

URL = "http://localhost:3333/mcp"

def test_mcp():
    headers = {
        "Accept": "text/event-stream"
    }
    try:
        print(f"Connecting to {URL} with Accept: text/event-stream...")
        # Stream=True for SSE
        with requests.get(URL, headers=headers, stream=True, timeout=5) as r:
            print(f"Status Code: {r.status_code}")
            print(f"Response Headers: {r.headers}")
            # Read first line
            for line in r.iter_lines():
                if line:
                    print(f"Response: {line.decode('utf-8')}")
                    break
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_mcp()
