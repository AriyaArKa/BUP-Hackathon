"""Keep-alive ping script for Render free tier deployment.

Render free instances spin down after 15 minutes of inactivity.
This script pings the /health endpoint every 10 minutes to ensure
the service remains awake and instantly responsive for judges.
"""

import sys
import time
from datetime import datetime
import httpx

DEFAULT_URL = "https://bup-hackathon-vimu.onrender.com"
PING_INTERVAL_SECONDS = 600  # 10 minutes (safe margin before Render's 15 min sleep)


def ping_health(url: str):
    target = url.rstrip("/") + "/health"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Pinging: {target} ...", end=" ", flush=True)
    try:
        t0 = time.time()
        resp = httpx.get(target, timeout=20.0)
        elapsed = time.time() - t0
        if resp.status_code == 200:
            print(f"OK (HTTP 200, {elapsed:.2f}s)")
        else:
            print(f"WARNING (HTTP {resp.status_code}, {elapsed:.2f}s)")
    except Exception as e:
        print(f"FAILED: {e}")


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print("=" * 60)
    print(" GridWise Render Keep-Alive Monitor")
    print(f" Target URL: {url}")
    print(f" Interval: Every {PING_INTERVAL_SECONDS // 60} minutes")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)

    while True:
        ping_health(url)
        time.sleep(PING_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nKeep-alive monitor stopped.")
