#!/usr/bin/env python3
"""
10XSEO Keyword Research API Proxy Server
Protects DataForSEO credentials from frontend exposure.
Run: python3 keyword-api-server.py
"""

import http.server
import json
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import base64
import time
import os
import mimetypes
import threading
from urllib.parse import urlparse, parse_qs
from collections import defaultdict
from datetime import datetime

# ============ CONFIGURATION ============
PORT = 3001
DATAFORSEO_LOGIN = "davit@10xseo.ge"
DATAFORSEO_PASSWORD = "fb35fc357556204b"
DATAFORSEO_AUTH = base64.b64encode(f"{DATAFORSEO_LOGIN}:{DATAFORSEO_PASSWORD}".encode()).decode()

CACHE_TTL = 3600  # 1 hour
TRENDS_CACHE_TTL = 86400  # 24 hours (Google Trends data changes slowly)
RATE_LIMIT = 30   # requests per minute per IP
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
TRENDS_MIN_INTERVAL = 5  # seconds between Google Trends API calls

# ============ IN-MEMORY STORES ============
cache = {}  # key -> {"data": ..., "expires": timestamp}
trends_cache = {}  # keyword -> {"data": ..., "expires": timestamp}
rate_limits = defaultdict(list)  # ip -> [timestamps]
last_trends_call = 0  # timestamp of last Google Trends API call
trends_lock = threading.Lock()  # thread safety for trends calls

# ============ TRENDS CACHE PERSISTENCE ============
# Save/load trends cache to disk so data survives server restarts.
# When Google Trends rate-limits us, cached data remains available.
TRENDS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trends_cache.json")


def _load_trends_cache():
    """Load trends cache from disk on startup."""
    global trends_cache
    try:
        if os.path.isfile(TRENDS_CACHE_FILE):
            with open(TRENDS_CACHE_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            now = time.time()
            loaded = 0
            for key, entry in disk.items():
                if entry.get("expires", 0) > now:
                    trends_cache[key] = entry
                    loaded += 1
            if loaded:
                print(f"[TRENDS] Loaded {loaded} cached entries from disk")
    except Exception as e:
        print(f"[TRENDS] Cache load error: {e}")


def _save_trends_cache():
    """Persist current trends cache to disk."""
    try:
        with open(TRENDS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(trends_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[TRENDS] Cache save error: {e}")


# Load any existing cache on startup
_load_trends_cache()

# ============ ALLOWED ORIGINS ============
ALLOWED_ORIGINS = [
    "http://localhost:3001",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3000",
    "https://10xseo.ge",
    "https://www.10xseo.ge",
    "http://10xseo.ge",
    "null",  # for file:// protocol
]


def get_cors_headers(origin=None):
    """Return CORS headers."""
    allowed = "*"
    if origin and origin in ALLOWED_ORIGINS:
        allowed = origin
    return {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }


def check_rate_limit(ip):
    """Return True if request is allowed, False if rate limited."""
    now = time.time()
    # Clean old entries
    rate_limits[ip] = [t for t in rate_limits[ip] if now - t < 60]
    if len(rate_limits[ip]) >= RATE_LIMIT:
        return False
    rate_limits[ip].append(now)
    return True


def get_cache(key):
    """Get cached response if not expired."""
    if key in cache and cache[key]["expires"] > time.time():
        return cache[key]["data"]
    if key in cache:
        del cache[key]
    return None


def set_cache(key, data):
    """Cache response with TTL."""
    cache[key] = {"data": data, "expires": time.time() + CACHE_TTL}


def dataforseo_request(endpoint, payload):
    """Make a request to DataForSEO API."""
    url = f"https://api.dataforseo.com/v3/{endpoint}"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {DATAFORSEO_AUTH}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else "{}"
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def has_georgian(text):
    """Check if text contains Georgian characters."""
    return any('\u10A0' <= ch <= '\u10FF' for ch in text)


def google_autocomplete(keyword, lang="ka", country="ge"):
    """Get Google Autocomplete suggestions — works for all languages including Georgian."""
    encoded = urllib.parse.quote(keyword)
    url = f"https://suggestqueries.google.com/complete/search?client=firefox&q={encoded}&hl={lang}&gl={country}"

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            suggestions = data[1] if len(data) > 1 else []
            # Filter out the original keyword
            return [s for s in suggestions if s.lower().strip() != keyword.lower().strip()]
    except Exception:
        return []


def google_autocomplete_expanded(keyword, lang="ka", country="ge"):
    """Get expanded suggestions by appending common prefixes/suffixes."""
    all_suggestions = set()

    # Base query
    base = google_autocomplete(keyword, lang, country)
    all_suggestions.update(base)

    # Append alphabet letters for more suggestions
    suffixes = [" ა", " ბ", " გ", " დ", " ე", " ვ", " ზ", " თ", " ი",
                " კ", " ლ", " მ", " ნ", " ო", " პ", " რ", " ს", " ტ", " უ", " ფ"]
    for suffix in suffixes[:10]:  # Limit to first 10 to avoid too many requests
        extra = google_autocomplete(keyword + suffix, lang, country)
        all_suggestions.update(extra)

    # Remove the original keyword
    all_suggestions.discard(keyword.lower().strip())
    return sorted(all_suggestions)


def google_autocomplete_with_scores(keyword, lang="ka", country="ge"):
    """Get expanded suggestions with popularity scores based on autocomplete ranking.

    Scores are calculated from:
    - Position in autocomplete results (earlier = more popular)
    - Whether keyword appears in base query (most important)
    - How many suffix queries return this keyword (frequency)
    Returns list of dicts with keyword, popularityScore (1-100), appearances.
    """
    keyword_data = defaultdict(lambda: {
        "count": 0, "best_position": 100, "in_base": False, "original": ""
    })

    # Base query (most important — these are the top suggestions)
    base = google_autocomplete(keyword, lang, country)
    for i, suggestion in enumerate(base):
        key = suggestion.lower().strip()
        keyword_data[key]["count"] += 2  # Base counts double
        keyword_data[key]["best_position"] = min(keyword_data[key]["best_position"], i)
        keyword_data[key]["in_base"] = True
        keyword_data[key]["original"] = suggestion

    # Suffix queries — Georgian alphabet letters
    suffixes = [" ა", " ბ", " გ", " დ", " ე", " ვ", " ზ", " თ", " ი",
                " კ", " ლ", " მ", " ნ", " ო", " პ", " რ", " ს", " ტ", " უ", " ფ"]
    for suffix in suffixes[:10]:
        results = google_autocomplete(keyword + suffix, lang, country)
        for i, suggestion in enumerate(results):
            key = suggestion.lower().strip()
            keyword_data[key]["count"] += 1
            keyword_data[key]["best_position"] = min(keyword_data[key]["best_position"], i)
            if not keyword_data[key]["original"]:
                keyword_data[key]["original"] = suggestion

    # Remove the original keyword itself
    keyword_data.pop(keyword.lower().strip(), None)

    if not keyword_data:
        return []

    max_count = max(d["count"] for d in keyword_data.values())

    results = []
    for key, data in keyword_data.items():
        # Score formula:
        # 50% from frequency (how many queries return this keyword)
        freq_score = (data["count"] / max(max_count, 1)) * 50
        # 35% from position (earlier in autocomplete = higher score)
        position_score = max(0, (10 - data["best_position"]) / 10) * 35
        # 15% bonus for appearing in base query (top suggestions)
        base_bonus = 15 if data["in_base"] else 0

        score = int(freq_score + position_score + base_bonus)
        score = max(1, min(100, score))  # Clamp 1-100

        results.append({
            "keyword": data["original"],
            "popularityScore": score,
            "inBase": data["in_base"],
            "appearances": data["count"],
        })

    # Sort by score descending
    results.sort(key=lambda x: x["popularityScore"], reverse=True)
    return results


def _build_trends_opener():
    """Build a fresh urllib opener with CookieJar for Google Trends requests.
    A persistent opener stores cookies across requests, mimicking a real browser session.
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return opener, cj


# Global persistent opener for Google Trends (reused across calls)
_trends_opener = None
_trends_opener_created = 0
_TRENDS_OPENER_TTL = 1800  # Recreate opener every 30 minutes


def _get_trends_opener():
    """Get or create the global Google Trends HTTP opener with cookie jar."""
    global _trends_opener, _trends_opener_created
    now = time.time()
    if _trends_opener is None or (now - _trends_opener_created) > _TRENDS_OPENER_TTL:
        _trends_opener, _ = _build_trends_opener()
        _trends_opener_created = now
        # Seed cookies by visiting the homepage (even 429 sets cookies)
        try:
            seed_req = urllib.request.Request("https://trends.google.com/trends/", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            _trends_opener.open(seed_req, timeout=10)
        except Exception:
            pass  # 429 is fine — cookies are still stored by the jar
        print("[TRENDS] Created fresh HTTP opener with cookie jar")
    return _trends_opener


def get_google_trends(keyword, geo="GE", timeframe="today 12-m"):
    """Get Google Trends interest data for a keyword.

    Uses a persistent HTTP session with cookie jar for reliable access.
    Two-step process: (1) explore endpoint for tokens, (2) multiline for data.
    Falls back to CSV download endpoint if multiline is rate-limited.

    Returns dict with:
      - interest: average interest (0-100)
      - monthlyData: list of {month, year, search_volume, interest} dicts
      - peak: max interest value
      - latest: most recent interest value
    Returns None if data unavailable or rate limited.
    """
    global last_trends_call

    # Check trends cache (24h TTL)
    cache_key = f"trends:{keyword.lower().strip()}:{geo}"
    if cache_key in trends_cache and trends_cache[cache_key]["expires"] > time.time():
        return trends_cache[cache_key]["data"]

    with trends_lock:
        # Double-check cache inside lock (another thread may have filled it)
        if cache_key in trends_cache and trends_cache[cache_key]["expires"] > time.time():
            return trends_cache[cache_key]["data"]

        # Throttle: minimum interval between Google Trends calls
        now = time.time()
        elapsed = now - last_trends_call
        if elapsed < TRENDS_MIN_INTERVAL:
            time.sleep(TRENDS_MIN_INTERVAL - elapsed)

        try:
            result = _fetch_google_trends_direct(keyword, geo, timeframe)
            last_trends_call = time.time()

            if result:
                trends_cache[cache_key] = {"data": result, "expires": time.time() + TRENDS_CACHE_TTL}
                _save_trends_cache()  # Persist to disk for restart resilience
            return result

        except Exception as e:
            last_trends_call = time.time()
            print(f"[TRENDS] Error for '{keyword}': {e}")
            return None


def _strip_google_prefix(raw):
    """Remove Google's anti-hijacking prefix from JSON responses."""
    if raw.startswith(")]}'"):
        return raw[5:] if raw[4:5] == "\n" else raw[4:]
    return raw


def _fetch_google_trends_direct(keyword, geo="GE", timeframe="today 12-m"):
    """Fetch Google Trends data using persistent opener with cookie jar.

    Uses a 2-step approach:
      Step 1: GET /trends/api/explore → widget tokens
      Step 2: GET /trends/api/widgetdata/multiline → timeseries data
    If Step 2 gets 429, falls back to CSV download endpoint.
    Retries Step 2 up to 2 times with exponential backoff.
    """
    opener = _get_trends_opener()
    base_url = "https://trends.google.com"

    encoded_kw = urllib.parse.quote(keyword)
    referer = f"{base_url}/trends/explore?geo={geo}&q={encoded_kw}"
    api_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,ka;q=0.8",
        "Referer": referer,
    }

    # ── Step 1: Explore endpoint (get widget tokens) ──
    req_json = json.dumps({
        "comparisonItem": [{"keyword": keyword, "geo": geo, "time": timeframe}],
        "category": 0,
        "property": ""
    })
    explore_url = f"{base_url}/trends/api/explore?hl=en&tz=-240&req={urllib.parse.quote(req_json)}"

    req = urllib.request.Request(explore_url, headers=api_headers)
    try:
        resp = opener.open(req, timeout=15)
        raw = resp.read().decode("utf-8")
        explore_data = json.loads(_strip_google_prefix(raw))
    except urllib.error.HTTPError as e:
        print(f"[TRENDS] Explore HTTP {e.code} for: {keyword}")
        if e.code == 429:
            # Force new opener on next call
            global _trends_opener
            _trends_opener = None
        return None
    except Exception as e:
        print(f"[TRENDS] Explore error for '{keyword}': {e}")
        return None

    # Find TIMESERIES widget token
    time_widget = None
    for w in explore_data.get("widgets", []):
        if w.get("id") == "TIMESERIES":
            time_widget = w
            break

    if not time_widget:
        print(f"[TRENDS] No TIMESERIES widget for: {keyword}")
        return None

    token = time_widget.get("token", "")
    widget_req = time_widget.get("request", {})

    # ── Step 2: Multiline data (with retry + CSV fallback) ──
    ml_url = (
        f"{base_url}/trends/api/widgetdata/multiline"
        f"?hl=en&tz=-240&token={token}"
        f"&req={urllib.parse.quote(json.dumps(widget_req))}"
    )

    ml_data = None
    for attempt in range(3):
        delay = 2 * (attempt + 1)  # 2s, 4s, 6s
        time.sleep(delay)

        ml_req = urllib.request.Request(ml_url, headers=api_headers)
        try:
            resp = opener.open(ml_req, timeout=15)
            raw = resp.read().decode("utf-8")
            ml_data = json.loads(_strip_google_prefix(raw))
            break  # Success
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"[TRENDS] Multiline 429, attempt {attempt + 1}/3 for: {keyword}")
                if attempt == 2:
                    # Final attempt failed — try CSV download fallback
                    print(f"[TRENDS] Trying CSV download fallback for: {keyword}")
                    ml_data = _try_csv_download_fallback(opener, api_headers, base_url, token, widget_req)
            else:
                print(f"[TRENDS] Multiline HTTP {e.code} for: {keyword}")
                break
        except Exception as e:
            print(f"[TRENDS] Multiline error: {e}")
            break

    if not ml_data:
        return None

    # ── Parse timeseries data ──
    return _parse_trends_timeline(ml_data, keyword)


def _try_csv_download_fallback(opener, headers, base_url, token, widget_req):
    """Try the CSV download endpoint as a fallback when multiline is rate-limited."""
    csv_url = (
        f"{base_url}/trends/api/widgetdata/multiline/csv"
        f"?hl=en&tz=-240&token={token}"
        f"&req={urllib.parse.quote(json.dumps(widget_req))}"
    )
    csv_req = urllib.request.Request(csv_url, headers=headers)
    try:
        resp = opener.open(csv_req, timeout=15)
        csv_text = resp.read().decode("utf-8")
        return _parse_csv_to_ml_format(csv_text)
    except Exception as e:
        print(f"[TRENDS] CSV fallback failed: {e}")
        return None


def _parse_csv_to_ml_format(csv_text):
    """Parse Google Trends CSV into the same format as multiline JSON response."""
    lines = csv_text.strip().split("\n")
    # CSV format: first 2-3 lines are headers, then "Week,keyword (GE)" then data
    data_start = -1
    for i, line in enumerate(lines):
        if line.startswith("Week,") or line.startswith("Day,"):
            data_start = i + 1
            break

    if data_start < 0 or data_start >= len(lines):
        return None

    timeline_data = []
    for line in lines[data_start:]:
        parts = line.strip().split(",")
        if len(parts) >= 2:
            date_str = parts[0].strip()
            try:
                val = int(parts[1].strip())
            except (ValueError, IndexError):
                continue
            # Parse date (YYYY-MM-DD format)
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                timeline_data.append({
                    "time": str(int(dt.timestamp())),
                    "value": [val],
                    "formattedTime": date_str,
                })
            except ValueError:
                continue

    if not timeline_data:
        return None

    return {"default": {"timelineData": timeline_data}}


def _parse_trends_timeline(ml_data, keyword):
    """Parse multiline JSON response into our standard trends format."""
    try:
        timeline_data = ml_data.get("default", {}).get("timelineData", [])
        if not timeline_data:
            return None

        monthly_data = []
        values = []
        for point in timeline_data:
            val = point.get("value", [0])[0]
            values.append(val)
            ts = int(point.get("time", 0))
            if ts > 0:
                dt = datetime.fromtimestamp(ts)
                monthly_data.append({
                    "month": dt.month,
                    "year": dt.year,
                    "search_volume": val,
                    "interest": val,
                })

        if not values:
            return None

        result = {
            "interest": round(sum(values) / len(values), 1),
            "monthlyData": monthly_data,
            "peak": max(values),
            "latest": values[-1],
            "dataPoints": len(values),
            "source": "google_trends",
        }
        print(f"[TRENDS] ✅ {keyword}: interest={result['interest']}, peak={result['peak']}, points={result['dataPoints']}")
        return result
    except Exception as e:
        print(f"[TRENDS] Parse error for '{keyword}': {e}")
        return None


def search_keyword(keyword, location_code=2268):
    """Search keyword volume and related keywords."""
    cache_key = f"{keyword.lower().strip()}:{location_code}"
    cached = get_cache(cache_key)
    if cached:
        return cached, True

    # Detect language for API call
    is_geo = has_georgian(keyword)
    # NOTE: DataForSEO recommends NOT sending language_code for Georgian (ka)
    # because Google Ads deprecated language filtering for many locales.
    # Sending language_code often returns null/0 volumes for Georgian keywords.
    # We only use location_code=2268 (Georgia) which is sufficient.

    # Call both endpoints
    volume_payload = [{
        "keywords": [keyword],
        "location_code": location_code,
        "sort_by": "search_volume",
    }]

    related_payload = [{
        "keywords": [keyword],
        "location_code": location_code,
        "sort_by": "search_volume",
        "limit": 50,
    }]

    volume_result = dataforseo_request(
        "keywords_data/google_ads/search_volume/live", volume_payload
    )
    related_result = dataforseo_request(
        "keywords_data/google_ads/keywords_for_keywords/live", related_payload
    )

    # Parse volume data
    main_kw = {}
    try:
        tasks = volume_result.get("tasks", [{}])
        if tasks and tasks[0].get("status_code") == 20000:
            results = tasks[0].get("result", [])
            if results and results[0]:
                r = results[0]
                main_kw = {
                    "keyword": r.get("keyword", keyword),
                    "searchVolume": r.get("search_volume") or 0,
                    "cpc": r.get("cpc") or 0,
                    "competition": r.get("competition"),
                    "competitionIndex": r.get("competition_index"),
                    "lowTopOfPageBid": r.get("low_top_of_page_bid") or 0,
                    "highTopOfPageBid": r.get("high_top_of_page_bid") or 0,
                    "monthlySearches": r.get("monthly_searches") or [],
                }
    except Exception as e:
        main_kw = {"error": f"Volume parse error: {str(e)}"}

    if not main_kw.get("keyword"):
        main_kw["keyword"] = keyword
    if "searchVolume" not in main_kw:
        main_kw["searchVolume"] = 0
        main_kw["monthlySearches"] = []

    # Parse related keywords
    related_keywords = []
    try:
        tasks = related_result.get("tasks", [{}])
        if tasks and tasks[0].get("status_code") == 20000:
            results = tasks[0].get("result", [])
            for r in (results or []):
                if r is None:
                    continue
                kw_name = r.get("keyword", "")
                if kw_name.lower().strip() == keyword.lower().strip():
                    continue  # Skip the original keyword
                related_keywords.append({
                    "keyword": kw_name,
                    "searchVolume": r.get("search_volume") or 0,
                    "cpc": r.get("cpc") or 0,
                    "competition": r.get("competition"),
                    "competitionIndex": r.get("competition_index"),
                })
    except Exception as e:
        pass

    # If Georgian keyword, use Google Autocomplete with popularity scoring
    if has_georgian(keyword):
        scored_suggestions = google_autocomplete_with_scores(keyword)
        main_kw["isGeorgian"] = True
        main_kw["autocompleteSource"] = True

        # --- Google Trends data (exact interest 0-100 from Google) ---
        trends_data = get_google_trends(keyword, geo="GE")
        if trends_data:
            main_kw["trendsInterest"] = trends_data["interest"]
            main_kw["trendsPeak"] = trends_data["peak"]
            main_kw["trendsLatest"] = trends_data["latest"]
            main_kw["monthlySearches"] = trends_data["monthlyData"]
            main_kw["trendsSource"] = True
            print(f"[TRENDS] {keyword}: interest={trends_data['interest']}, peak={trends_data['peak']}")
        else:
            main_kw["trendsSource"] = False

        if scored_suggestions and (not related_keywords or len(related_keywords) == 0):
            # Try to get Google Ads volumes for top suggestions (with language_code for Georgian)
            batch_kws = [s["keyword"] for s in scored_suggestions[:50]]
            ads_volumes = {}  # keyword_lower -> {volume, cpc, competition, ...}

            if batch_kws:
                batch_payload = [{
                    "keywords": batch_kws,
                    "location_code": location_code,
                    # No language_code — DataForSEO recommends omitting it for Georgian
                    "sort_by": "search_volume"
                }]
                batch_result = dataforseo_request(
                    "keywords_data/google_ads/search_volume/live", batch_payload
                )
                try:
                    tasks = batch_result.get("tasks", [{}])
                    if tasks and tasks[0].get("status_code") == 20000:
                        batch_items = tasks[0].get("result", [])
                        for r in (batch_items or []):
                            if r is None:
                                continue
                            kw_name = r.get("keyword", "")
                            ads_volumes[kw_name.lower().strip()] = {
                                "searchVolume": r.get("search_volume") or 0,
                                "cpc": r.get("cpc") or 0,
                                "competition": r.get("competition"),
                                "competitionIndex": r.get("competition_index"),
                            }
                except Exception:
                    pass

            # Build related keywords with popularity scores
            for s in scored_suggestions:
                kw_lower = s["keyword"].lower().strip()
                if kw_lower == keyword.lower().strip():
                    continue

                ads_data = ads_volumes.get(kw_lower, {})
                related_keywords.append({
                    "keyword": s["keyword"],
                    "searchVolume": ads_data.get("searchVolume", 0),
                    "cpc": ads_data.get("cpc", 0),
                    "competition": ads_data.get("competition"),
                    "competitionIndex": ads_data.get("competitionIndex"),
                    "popularityScore": s["popularityScore"],
                    "source": "autocomplete",
                })

        # Set main keyword popularity to 100 (it's the seed keyword)
        main_kw["popularityScore"] = 100

    main_kw["relatedKeywords"] = related_keywords
    main_kw["totalRelated"] = len(related_keywords)

    # Cache result
    set_cache(cache_key, main_kw)
    return main_kw, False


class KeywordAPIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for keyword research API."""

    def log_message(self, format, *args):
        """Custom logging."""
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} - {format % args}")

    def send_json(self, data, status=200):
        """Send JSON response."""
        origin = self.headers.get("Origin", "*")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        for key, val in get_cors_headers(origin).items():
            self.send_header(key, val)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        origin = self.headers.get("Origin", "*")
        self.send_response(204)
        for key, val in get_cors_headers(origin).items():
            self.send_header(key, val)
        self.end_headers()

    def do_GET(self):
        """Handle GET requests — static files + health endpoint."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json({
                "status": "ok",
                "cacheSize": len(cache),
                "port": PORT,
            })
            return

        # Serve static files
        if path == "/":
            path = "/keyword-research.html"

        file_path = os.path.join(STATIC_DIR, path.lstrip("/"))
        file_path = os.path.realpath(file_path)

        # Security: prevent directory traversal
        if not file_path.startswith(os.path.realpath(STATIC_DIR)):
            self.send_error(403, "Forbidden")
            return

        if os.path.isfile(file_path):
            content_type, _ = mimetypes.guess_type(file_path)
            if content_type is None:
                content_type = "application/octet-stream"

            try:
                with open(file_path, "rb") as f:
                    content = f.read()

                self.send_response(200)
                origin = self.headers.get("Origin", "*")
                for key, val in get_cors_headers(origin).items():
                    self.send_header(key, val)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404, "File not found")

    def do_POST(self):
        """Handle POST requests — API endpoints."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/keyword-search":
            self.handle_keyword_search()
        elif path == "/api/cache-clear":
            cache.clear()
            self.send_json({"status": "ok", "message": "Cache cleared"})
        else:
            self.send_json({"error": "Not found"}, 404)

    def handle_keyword_search(self):
        """Handle keyword search request."""
        # Rate limit check
        client_ip = self.client_address[0]
        if not check_rate_limit(client_ip):
            self.send_json({"error": "Rate limit exceeded. Max 30 requests/minute."}, 429)
            return

        # Read body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 1048576:  # 1MB limit
                self.send_json({"error": "Request too large"}, 413)
                return
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, Exception) as e:
            self.send_json({"error": f"Invalid JSON: {str(e)}"}, 400)
            return

        keyword = data.get("keyword", "").strip()
        if not keyword:
            self.send_json({"error": "Missing 'keyword' field"}, 400)
            return

        location_code = data.get("location_code", 2268)

        # Search
        try:
            result, from_cache = search_keyword(keyword, location_code)
            if from_cache:
                self.log_message("CACHE HIT: %s", keyword)
            else:
                self.log_message("API CALL: %s (volume=%s, related=%s)",
                    keyword,
                    result.get("searchVolume", "?"),
                    result.get("totalRelated", "?"))
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": f"Server error: {str(e)}"}, 500)


def main():
    server = http.server.HTTPServer(("0.0.0.0", PORT), KeywordAPIHandler)
    print(f"""
╔══════════════════════════════════════════════════╗
║   10XSEO Keyword Research API Proxy Server       ║
║   Port: {PORT}                                      ║
╠══════════════════════════════════════════════════╣
║   Endpoints:                                     ║
║   POST /api/keyword-search                       ║
║   GET  /api/health                               ║
║   POST /api/cache-clear                          ║
║                                                  ║
║   Static files served from:                      ║
║   {STATIC_DIR[:46]:46} ║
║                                                  ║
║   Open: http://localhost:{PORT}/keyword-research.html ║
╚══════════════════════════════════════════════════╝
    """)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
