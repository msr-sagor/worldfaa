import json
import re
import requests
import base64

SESSION = requests.Session()

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
    """
    Decode eval(function(h,u,n,t,e,r){...}) packed payload
    """
    result = ""
    i = 0

    if e >= len(n):
        raise ValueError(f"Invalid e index: {e}, n length: {len(n)}")

    delimiter = n[e]
    n_map = {char: str(idx) for idx, char in enumerate(n)}

    while i < len(h):
        token = ""
        while i < len(h) and h[i] != delimiter:
            token += h[i]
            i += 1

        i += 1  # skip delimiter

        if token:
            token_digits = "".join(n_map.get(c, c) for c in token)
            try:
                char_code = int(convert_base(token_digits, e, 10)) - t
                result += chr(char_code)
            except Exception:
                # skip broken token instead of crashing entire run
                continue

    return result


def safe_b64_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    while len(s) % 4:
        s += "="
    return base64.b64decode(s).decode("utf-8", errors="ignore")


def extract_eval_params(html):
    """
    Extract packed JS parameters robustly.
    Supports:
    eval(function(h,u,n,t,e,r){...}('...',123,'...',10,62,...))
    """
    match = re.search(
        r"""eval\s*\(\s*function\s*\(\s*h\s*,\s*u\s*,\s*n\s*,\s*t\s*,\s*e\s*,\s*r\s*\)\s*\{.*?\}\s*\((.*?)\)\s*\)""",
        html,
        re.DOTALL,
    )
    if not match:
        return None

    params_str = match.group(1)

    # More flexible parsing
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
    """
    Extract final m3u8 URL from deobfuscated JS
    """
    # find source key
    src_var_match = re.search(r'''["']?src["']?\s*:\s*([A-Za-z_]\w*)''', js_code)
    if not src_var_match:
        return None

    src_var_name = src_var_match.group(1)

    # support const / let / var
    assign_match = re.search(
        rf'''(?:const|let|var)\s+{re.escape(src_var_name)}\s*=\s*(.*?);''',
        js_code,
        re.DOTALL,
    )
    if not assign_match:
        return None

    assignment_expr = assign_match.group(1)

    # decoder function
    decoder_func_match = re.search(
        r'''function\s+([A-Za-z_]\w*)\s*\(\s*\w+\s*\)''',
        js_code
    )
    if not decoder_func_match:
        return None

    decoder_name = decoder_func_match.group(1)

    # variables passed into decoder calls
    parts_vars = re.findall(
        rf'''{re.escape(decoder_name)}\(\s*([A-Za-z_]\w*)\s*\)''',
        assignment_expr
    )
    if not parts_vars:
        return None

    # support single or double quotes
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


def get_m3u8_url(channel_url, referer):
    headers = {
        **DEFAULT_HEADERS,
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    try:
        response = SESSION.get(channel_url, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text

        params = extract_eval_params(html)
        if not params:
            print(f"[FAIL] Packed script not found: {channel_url}")
            return None

        h, n, t, e = params

        try:
            js_code = deobfuscate(h, n, t, e)
        except Exception as ex:
            print(f"[FAIL] Deobfuscation failed for {channel_url}: {ex}")
            return None

        final_url = extract_player_url_from_code(js_code)
        if not final_url:
            print(f"[FAIL] Could not extract source URL from JS: {channel_url}")
            return None

        if ".m3u8" not in final_url:
            print(f"[WARN] Extracted URL is not m3u8: {final_url}")

        return final_url

    except requests.exceptions.RequestException as ex:
        print(f"[FAIL] Request error for {channel_url}: {ex}")
        return None
    except Exception as ex:
        print(f"[FAIL] Unexpected error for {channel_url}: {ex}")
        return None


def get_online_channels(referer):
    api_url = "https://api.cdn-live.tv/api/v1/channels/?user=streamsports99&plan=vip"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    try:
        response = SESSION.get(api_url, headers=headers, timeout=20)
        response.raise_for_status()

        data = response.json()
        channels = data.get("channels", [])
        if not isinstance(channels, list):
            print("[FAIL] API returned invalid channel list")
            return []

        online_channels = [ch for ch in channels if str(ch.get("status", "")).lower() == "online"]

        sports_keywords = [
            "sport", "sports", "football", "cricket", "espn", "wwe",
            "premier league", "liga", "dazn", "tnt sports", "sky sports",
            "bein sports", "fox sports", "supersport", "arena", "match",
            "sportv", "premier", "tyc sports", "eleven sports",
            "polsat sport", "ssc", "sony ten", "eurosport"
        ]

        sports_channels = []
        seen = set()

        for channel in online_channels:
            name = str(channel.get("name", "")).strip()
            name_l = name.lower()

            if any(keyword in name_l for keyword in sports_keywords):
                key = (name, channel.get("url"))
                if key not in seen:
                    seen.add(key)
                    sports_channels.append(channel)

        print(f"[INFO] Total channels from API: {len(channels)}")
        print(f"[INFO] Online channels: {len(online_channels)}")
        print(f"[INFO] Sports channels: {len(sports_channels)}")

        return sports_channels

    except requests.exceptions.RequestException as ex:
        print(f"[FAIL] Channel API request error: {ex}")
        return []
    except json.JSONDecodeError as ex:
        print(f"[FAIL] JSON decode error: {ex}")
        return []
    except Exception as ex:
        print(f"[FAIL] Unexpected API error: {ex}")
        return []


def create_playlist():
    referer_url = "https://edge.cdn-live.ru/"
    channels = get_online_channels(referer_url)

    if not channels:
        print("[FAIL] No online sports channels found.")
        return

    ok_count = 0

    with open("worldsp.m3u", "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url="https://github.com/epgshare01/share/raw/master/epg_ripper_ALL_SOURCES1.xml.gz"\n')

        for channel in channels:
            name = channel.get("name", "Unknown")
            code = channel.get("code", "")
            logo = channel.get("image", "")
            player_page_url = channel.get("url")

            print(f"[INFO] Processing: {name}")

            if not player_page_url:
                print(f"[SKIP] Missing player page URL: {name}")
                continue

            m3u8_url = get_m3u8_url(player_page_url, referer_url)

            if not m3u8_url:
                print(f"[SKIP] Could not resolve stream: {name}")
                continue

            f.write(
                f'#EXTINF:-1 tvg-id="{code}" tvg-name="{name}" tvg-logo="{logo}" group-title="Sports",{name}\n'
            )
            f.write(f'#EXTVLCOPT:http-referrer={referer_url}\n')
            f.write(f'#EXTVLCOPT:http-user-agent={DEFAULT_HEADERS["User-Agent"]}\n')
            f.write(f"{m3u8_url}\n")

            ok_count += 1

    if ok_count == 0:
        print("[FAIL] Playlist file created, but no working channels were resolved.")
    else:
        print(f"[SUCCESS] Playlist created successfully with {ok_count} channels.")


if __name__ == "__main__":
    create_playlist()
