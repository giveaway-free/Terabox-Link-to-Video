import os
import time
import requests
import concurrent.futures
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, Any, Optional, List

API_ENDPOINT = "https://api.teraboxdl.site/api/test"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_cached_working_proxy: Optional[str] = None
_proxy_pool: List[str] = []
_proxy_pool_time: float = 0

def fetch_fresh_proxies() -> List[str]:
    global _proxy_pool, _proxy_pool_time
    now = time.time()
    if _proxy_pool and (now - _proxy_pool_time < 300):
        return _proxy_pool

    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=yes&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    ]
    proxies = []
    for src in sources:
        try:
            r = requests.get(src, timeout=4)
            if r.status_code == 200:
                lines = [line.strip() for line in r.text.splitlines() if line.strip() and ":" in line]
                proxies.extend(lines)
                if len(proxies) >= 30:
                    break
        except Exception:
            continue

    _proxy_pool = list(dict.fromkeys(proxies))[:50]
    _proxy_pool_time = now
    return _proxy_pool

def _try_single_request(url: str, proxy: Optional[str] = None, timeout: int = 6) -> Optional[dict]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {"url": url.strip()}
    proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy and not proxy.startswith("http") else ({"http": proxy, "https": proxy} if proxy else None)

    try:
        r = requests.post(API_ENDPOINT, json=payload, headers=headers, proxies=proxies, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success" and data.get("data", {}).get("list"):
                return data
    except Exception:
        pass
    return None

def resolve_via_rotating_proxies(share_url: str) -> dict:
    global _cached_working_proxy
    if _cached_working_proxy:
        data = _try_single_request(share_url, _cached_working_proxy, timeout=5)
        if data:
            return data

    proxies = fetch_fresh_proxies()
    if not proxies:
        raise RuntimeError("Could not retrieve active proxies for IP bypass.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        future_to_proxy = {executor.submit(_try_single_request, share_url, p, 6): p for p in proxies[:25]}
        for future in concurrent.futures.as_completed(future_to_proxy):
            p = future_to_proxy[future]
            try:
                data = future.result()
                if data:
                    _cached_working_proxy = p
                    return data
            except Exception:
                continue

    raise RuntimeError("All proxy rotation nodes failed.")

def resolve_terabox_link(share_url: str) -> Dict[str, Any]:
    url_str = share_url.strip()

    # 1. Player check
    if "player.teraboxdl.site" in url_str:
        parsed = urlparse(url_str)
        qs = parse_qs(parsed.query)
        direct = qs.get("direct", [""])[0]
        filename = qs.get("filename", ["video.mp4"])[0]
        size_str = qs.get("size", [""])[0]
        poster = qs.get("poster", [None])[0]
        return {
            "filename": filename,
            "size": 0,
            "formatted_size": size_str or "--",
            "direct_link": direct,
            "thumbnail_url": poster,
        }

    # 2. Worker check
    if "dl-worker.teraboxdl.site" in url_str:
        return {
            "filename": "video.mp4",
            "size": 0,
            "formatted_size": "--",
            "direct_link": url_str,
            "thumbnail_url": None,
        }

    # 3. Standard
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    payload = {"url": url_str}
    data = None
    try:
        r = requests.post(API_ENDPOINT, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            resp_json = r.json()
            if resp_json.get("status") == "success" and resp_json.get("data", {}).get("list"):
                data = resp_json
        else:
            data = resolve_via_rotating_proxies(url_str)
    except Exception:
        data = resolve_via_rotating_proxies(url_str)

    if not data or data.get("status") != "success":
        raise RuntimeError("Failed to resolve TeraBox link.")

    file_list = data.get("data", {}).get("list", [])
    if not file_list:
        raise ValueError("No files found in the provided TeraBox link.")

    f = file_list[0]
    direct_link = f.get("direct_link") or f.get("stream_download_url") or f.get("dlink")
    thumbs = f.get("thumbs", {})
    thumbnail = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1")

    return {
        "filename": f.get("server_filename", "video.mp4"),
        "size": int(f.get("size", 0)),
        "formatted_size": f.get("formatted_size", f"{int(f.get('size', 0)) / (1024*1024):.2f} MB"),
        "direct_link": direct_link,
        "thumbnail_url": thumbnail
    }
