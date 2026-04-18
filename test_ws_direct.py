import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://127.0.0.1:8000/api/v1/ws/status"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected!")
            # Should receive initial status push
            message = await asyncio.wait_for(websocket.recv(), timeout=5)
            print(f"Received status: {message}")
            data = json.loads(message)
            if data['equity'] >= 100000.0:
                print("SUCCESS: Equity is correct!")
            else:
                print(f"FAILURE: Equity is {data['equity']}")
    except Exception as e:
        print(f"websocket error: {e}")

async def test_logs_ws():
    uri = "ws://127.0.0.1:8000/api/v1/ws/logs"
    print(f"Connecting to {uri}...")
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to logs!")
            # Wait for some logs (engine logs every loop, and we triggered one at startup)
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            print(f"Received log: {message}")
            if "!!!" in message or "DEBUG" in message:
                print("SUCCESS: Logs are flowing!")
    except Exception as e:
        print(f"websocket logs error: {e}")

async def main():
    await test_ws()
    await test_logs_ws()

if __name__ == "__main__":
    asyncio.run(main())
