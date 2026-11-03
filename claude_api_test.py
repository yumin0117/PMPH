import getpass
from anthropic import Anthropic

print("=== Claude API Test ===")

key = getpass.getpass("Claude API Key: ").strip()

if not key:
    print("[ERROR] API key is empty.")
else:
    try:
        client = Anthropic(api_key=key, timeout=30.0)

        response = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=100,
            messages=[
                {
                    "role": "user",
                    "content": "Reply only with: CONNECTED"
                }
            ]
        )

        print("[SUCCESS] Claude API connected!")
        for block in response.content:
            if block.type == "text":
                print("Response:", block.text)

        print("Model:", response.model)

    except Exception as e:
        print("[ERROR]", type(e).__name__)
        print(str(e))
