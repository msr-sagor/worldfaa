import json
import re
import requests
import base64

SESSION = requests.Session()

API_URL = "https://api.cdn-live.tv/api/v1/channels/?user=streamsports99&plan=vip"
REFERER_URL = "https://edge.cdn-live.ru/"
OUTPUT_FILE = "worldsp.m3u"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def convert_base(num_str, from_base, to_base):
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
    from_chars = chars[:from_base]
    to_chars = chars[:to_base]

    value = 0
    for power, ch in enumerate(num_str[::-1]):
        idx = from_chars.find(ch)
        if idx == -1:
            raise ValueError(f"Invalid character '{ch}' for base {from_base}")
        value += idx * (from_base ** power)

    if value == 0:
        return "0"

    out = ""
    while value > 0:
        out = to_chars[value % to_base] + out
        value //= to_base
    return out


def deobfuscate(h, n, t, e):
    result = ""
    i = 0

    if not n or e >= len(n):
        return result

    delimiter = n[e]
    n_map = {char: str(idx) for idx, char in enumerate(n)}

    while i < len(h):
        token = ""
        while i < len(h) and h[i] != delimiter:
            token += h[i]
            i += 1

        i += 1

        if token:
            token_digits = "".join(n_map.get(c, c) for c in token)
            try:
                char_code = int(convert_base(token_digits, e, 10)) - t
                result += chr(char_code)
            except Exception:
                continue

    return result


def safe_b64_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    while len(s) % 4:
        s += "="
    return base64.b64decode(s).decode("utf-8", errors="ignore")


def extract_eval_params(html):
    match = re.search(
        r"""eval\s*\(\s*function\s*\(\s*h\s*,\s*u\s*,\s*n\s*,\s*t\s*,\s*e\s*,\s*r\s*\)\s*\{.*?\}\s*\((.*?)\)\s*\)""",
        html,
        re.DOTALL,
    )
    if not match:
        return None

    params_str = match.group(1)

    params_match = re.search(
        r"""(['"])(.*?)\1\s*,\s*\d+\s*,\s*(['"])(.*?)\3\s*,\s*(\d+)\s*,\s*(\d+)""",
        params_str,
        re.DOTALL,
    )
    if not params_match:
        return None

    h = params_match.group(2)
    n = params_match.group(4)
    t = int(params_match.group(5))
    e = int(params_match.group(6))
    return h, n, t, e


def extract_player_url_from_code(js_code):
    direct_url_match = re.search(r'https?://[^\s\'"]+\.m3u8[^\s\'"]*', js_code)
    if direct_url_match:
        return direct_url_match.group(0)

    src_var_match = re.search(r'''["']?src["']?\s*:\s*([A-Za-z_]\w*)''', js_code)
    if not src_var_match:
        return None

    src_var_name = src_var_match.group(1)

    assign_match = re.search(
        rf'''(?:const|let|var)\s+{re.escape(src_var_name)}\s*=\s*(.*?);''',
        js_code,
        re.DOTALL,
    )
    if not assign_match:
        return None

    assignment_expr = assign_match.group(1)

    decoder_func_match = re.search(
        r'''function\s+([A-Za-z_]\w*)\s*\(\s*\w+\s*\)''',
        js_code
    )
    if not decoder_func_match:
        return None

    decoder_name = decoder_func_match.group(1)

    parts_vars = re.findall(
        rf'''{re.escape(decoder_name)}\(\s*([A-Za-z_]\w*)\s*\)''',
        assignment_expr
    )
    if not parts_vars:
        return None

    declarations = dict(
        re.findall(
            r'''(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*['"]([^'"]+)['"]\s*;''',
            js_code
        )
    )

    url_parts_b64 = []
    for var_name in parts_vars:
        if var_name not in declarations:
            return None
        url_parts_b64.append(declarations[var_name])

    try:
        decoded_parts = [safe_b64_decode(p) for p in url_parts_b64]
        return "".join(decoded_parts)
    except Exception:
        return None


def get_m3u8_url_from_player_page(channel_url, referer):
    headers = {
        **DEFAULT_HEADERS,
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    try:
        response = SESSION.get(channel_url, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text

        direct_m3u8 = re.search(r'https?://[^\s\'"]+\.m3u8[^\s\'"]*', html)
        if direct_m3u8:
            return direct_m3u8.group(0)

        params = extract_eval_params(html)
        if not params:
            print(f"[FAIL] Packed script not found: {channel_url}")
            return None

        h, n, t, e = params
        js_code = deobfuscate(h, n, t, e)

        final_url = extract_player_url_from_code(js_code)
        if not final_url:
            print(f"[FAIL] Could not extract source URL: {channel_url}")
            return None

        return final_url

    except requests.exceptions.RequestException as ex:
        print(f"[FAIL] Request error for player page {channel_url}: {ex}")
        return None
    except Exception as ex:
        print(f"[FAIL] Unexpected error for player page {channel_url}: {ex}")
        return None


def deep_find_channel_list(obj):
    if isinstance(obj, dict):
        for key in ["channels", "data", "results", "items", "list"]:
            if key in obj and isinstance(obj[key], list):
                return obj[key]

        for value in obj.values():
            found = deep_find_channel_list(value)
            if found:
                return found

    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            sample_keys = set()
            for item in obj[:5]:
                sample_keys.update(item.keys())
            if {"name", "url"} & sample_keys:
                return obj

        for item in obj:
            found = deep_find_channel_list(item)
            if found:
                return found

    return []


def get_any(d, keys, default=""):
    for key in keys:
        if key in d and d[key] not in [None, ""]:
            return d[key]
    return default


def normalize_channel(raw):
    if not isinstance(raw, dict):
        return None

    name = get_any(raw, ["name", "title", "channel_name", "label"], "Unknown")
    code = get_any(raw, ["code", "id", "slug", "channel_id"], "")
    logo = get_any(raw, ["image", "logo", "icon", "poster", "thumbnail"], "")
    status = str(get_any(raw, ["status", "state", "live_status"], "")).lower()

    player_url = get_any(
        raw,
        ["url", "link", "player_url", "watch_url", "page", "embed_url"],
        ""
    )

    stream_url = get_any(
        raw,
        ["m3u8", "m3u8_url", "stream_url", "source", "file", "src"],
        ""
    )

    return {
        "name": str(name).strip(),
        "code": str(code).strip(),
        "logo": str(logo).strip(),
        "status": status,
        "player_url": str(player_url).strip(),
        "stream_url": str(stream_url).strip(),
        "raw": raw,
    }


def is_online_channel(ch):
    status = ch.get("status", "")
    if not status:
        return True
    return status in ["online", "live", "on", "active", "available"]


def is_sports_channel(name):
    name = name.lower()
    sports_keywords = [
        "sport", "sports", "football", "cricket", "espn", "wwe", "ufc",
        "premier league", "liga", "dazn", "tnt sports", "sky sports",
        "bein sports", "fox sports", "supersport", "arena", "match",
        "sportv", "premier", "tyc sports", "eleven sports", "polsat sport",
        "ssc", "sony ten", "eurosport", "star sports", "astro cricket"
    ]
    return any(k in name for k in sports_keywords)


def get_channels_from_api():
    headers = {
        **DEFAULT_HEADERS,
        "Referer": REFERER_URL,
        "Origin": REFERER_URL.rstrip("/"),
    }

    try:
        response = SESSION.get(API_URL, headers=headers, timeout=20)
        response.raise_for_status()

        print("[INFO] API status:", response.status_code)
        print("[INFO] Content-Type:", response.headers.get("Content-Type", ""))

        data = response.json()

        with open("api_response_preview.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        raw_channels = deep_find_channel_list(data)
        if not raw_channels:
            print("[FAIL] No channel list found in API response.")
            return []

        normalized = []
        seen = set()

        for raw in raw_channels:
            ch = normalize_channel(raw)
            if not ch:
                continue

            if not is_online_channel(ch):
                continue

            if not is_sports_channel(ch["name"]):
                continue

            key = (ch["name"], ch["player_url"], ch["stream_url"])
            if key in seen:
                continue
            seen.add(key)

            normalized.append(ch)

        print(f"[INFO] Total raw channels found: {len(raw_channels)}")
        print(f"[INFO] Sports + online channels: {len(normalized)}")
        return normalized

    except requests.exceptions.RequestException as ex:
        print(f"[FAIL] API request error: {ex}")
        return []
    except json.JSONDecodeError as ex:
        print(f"[FAIL] JSON decode error: {ex}")
        return []
    except Exception as ex:
        print(f"[FAIL] Unexpected API error: {ex}")
        return []


def resolve_stream_url(channel):
    name = channel["name"]

    if channel["stream_url"]:
        stream_url = channel["stream_url"]
        if ".m3u8" in stream_url:
            print(f"[OK] Direct stream found for {name}")
            return stream_url

    if channel["player_url"]:
        print(f"[INFO] Resolving player page for {name}")
        return get_m3u8_url_from_player_page(channel["player_url"], REFERER_URL)

    print(f"[SKIP] No stream_url or player_url for {name}")
    return None


def create_playlist():
    channels = get_channels_from_api()
    if not channels:
        print("[FAIL] No channels found.")
        return

    ok_count = 0

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url="https://github.com/epgshare01/share/raw/master/epg_ripper_ALL_SOURCES1.xml.gz"\n')

        for channel in channels:
            name = channel["name"]
            code = channel["code"]
            logo = channel["logo"]

            print(f"[INFO] Processing: {name}")
            stream_url = resolve_stream_url(channel)

            if not stream_url:
                print(f"[SKIP] Could not resolve stream for {name}")
                continue

            f.write(
                f'#EXTINF:-1 tvg-id="{code}" tvg-name="{name}" tvg-logo="{logo}" group-title="Sports",{name}\n'
            )
            f.write(f'#EXTVLCOPT:http-referrer={REFERER_URL}\n')
            f.write(f'#EXTVLCOPT:http-user-agent={DEFAULT_HEADERS["User-Agent"]}\n')
            f.write(f"{stream_url}\n")

            ok_count += 1

    if ok_count == 0:
        print("[FAIL] Playlist created, but no working streams were resolved.")
    else:
        print(f"[SUCCESS] Playlist created successfully with {ok_count} channels.")


if __name__ == "__main__":
    create_playlist()