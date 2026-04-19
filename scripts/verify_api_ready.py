import requests
import time
import sys

def verify():
    print("--- STARTING API VERIFICATION ---")
    urls = [
        "http://localhost:8000/api/v1/status",
        "http://localhost:8000/api/v1/klines?symbol=BTC/USDT",
        "http://localhost:8000/api/v1/health"
    ]
    
    # Wait for service to start if needed (manual trigger expected)
    for url in urls:
        try:
            print(f"Testing {url}...")
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                print(f"  [OK] Status: 200")
                if "klines" in url:
                    print(f"  [OK] Data length: {len(data)}")
                    if len(data) == 0:
                        print("  [WARN] K-line cache is empty. Backend might still be fetching.")
                else:
                    print(f"  [OK] Response: {str(data)[:100]}...")
            else:
                print(f"  [FAILED] Status: {resp.status_code}")
        except Exception as e:
            print(f"  [ERROR] {e}")

if __name__ == "__main__":
    verify()
