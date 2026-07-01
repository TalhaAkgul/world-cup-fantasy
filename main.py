#!/usr/bin/env python3
"""
FIFA Fantasy League Tracker — Flask Web App
============================================
Run locally:   python app.py
Deploy:        gunicorn app:app   (see Procfile)

Public calls (no cookie):
    /json/fantasy/players.json
    /json/fantasy/squads.json
    /json/fantasy/rounds.json

Authenticated calls (cookie required):
    /api/en/fantasy/ranking/league/<id>
    /api/en/fantasy/team/history/<round>/<userId>

Cookie is stored server-side in cookie.txt and refreshed via the web UI.
Static data auto-refreshes every 60 s while any match is live.
"""

import gzip
import json
import math
import os
import threading
import time
import zlib
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False
    from urllib import request as urlrequest, error as urlerror

from flask import Flask, jsonify, request, render_template

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "https://play.fifa.com"
STATIC_FILES = {
    "players": "/json/fantasy/players.json",
    "squads":  "/json/fantasy/squads.json",
    "rounds":  "/json/fantasy/rounds.json",
}
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "fifa_cache")
COOKIE_FILE = os.path.join(HERE, "cookie.txt")

DEFAULT_LEAGUE_ID = 34608
DEFAULT_LIMIT = 20
TURKEY_TZ = timezone(timedelta(hours=3))  # TRT = UTC+3 (no DST since 2016)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
POS_ORDER = ["GK", "DEF", "MID", "FWD"]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def http_get_json(url, cookie=None, timeout=30):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://play.fifa.com/",
    }

    def do_fetch(cookie_to_use):
        headers_to_use = headers.copy()
        if cookie_to_use:
            headers_to_use["Cookie"] = cookie_to_use.strip()

        if HAVE_REQUESTS:
            r = requests.get(url, headers=headers_to_use, timeout=timeout)
            if r.status_code >= 400:
                print(f"[auth] Request failed for URL {url}: HTTP {r.status_code}. Response: {r.text[:500]}", flush=True)
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()

        headers_to_use["Accept-Encoding"] = "gzip, deflate"
        req = urlrequest.Request(url, headers=headers_to_use)
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
        except urlerror.HTTPError as e:
            try:
                err_body = e.read()
                print(f"[auth] Request failed for URL {url}: HTTP {e.code}. Response: {err_body[:500]}", flush=True)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}")
        except urlerror.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")

        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return json.loads(raw.decode("utf-8", errors="replace"))

    try:
        return do_fetch(cookie)
    except RuntimeError as e:
        if ("HTTP 401" in str(e) or "HTTP 403" in str(e)) and cookie:
            print("[auth] API request failed with 401/403. Reloading cookie...", flush=True)
            
            global _cookie_cache
            _cookie_cache = None
            if "FIFA_COOKIE" in os.environ:
                del os.environ["FIFA_COOKIE"]
            if "COOKIE" in os.environ:
                del os.environ["COOKIE"]
            if "FIFA_COOKIE_EXPIRES" in os.environ:
                del os.environ["FIFA_COOKIE_EXPIRES"]
                
            new_cookie = _load_cookie()
            if new_cookie and new_cookie != cookie:
                print("[auth] Retrying with reloaded cookie...", flush=True)
                return do_fetch(new_cookie)
            else:
                print("[auth] Cookie is expired or invalid. Please update the cookie using the web UI.", flush=True)
        raise


# ---------------------------------------------------------------------------
# Match helpers — handle various FIFA API shapes
# ---------------------------------------------------------------------------
def _get_round_matches(round_data):
    return (round_data.get("tournaments") or
            round_data.get("matches") or
            round_data.get("fixtures") or
            round_data.get("games") or [])


def _get_match_teams(match):
    home = (match.get("homeSquadId") or match.get("homeTeamId") or
            (match.get("home") or {}).get("id") or match.get("squadId1"))
    away = (match.get("awaySquadId") or match.get("awayTeamId") or
            (match.get("away") or {}).get("id") or match.get("squadId2"))
    return home, away


def _parse_match_time(match):
    raw = (match.get("startTime") or match.get("start") or
           match.get("date") or match.get("kickoff") or match.get("matchDate"))
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------
class DataStore:
    def __init__(self):
        self.players = {}
        self.squads = {}
        self.rounds = []
        self._lock = threading.RLock()
        self.last_refresh = None
        self.ready = False

    def load_static(self, force=False, log=lambda m: None):
        os.makedirs(CACHE_DIR, exist_ok=True)
        raw = {}
        for key, path in STATIC_FILES.items():
            cache_path = os.path.join(CACHE_DIR, key + ".json")
            data = None
            if not force and os.path.exists(cache_path):
                try:
                    with open(cache_path, encoding="utf-8") as f:
                        data = json.load(f)
                    log(f"Loaded {key} from cache")
                except Exception:
                    pass
            if data is None:
                log(f"Downloading {key}.json …")
                data = http_get_json(BASE + path)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                log(f"Saved {key}.json")
            raw[key] = data

        with self._lock:
            self.players = {p["id"]: p for p in raw["players"]}
            self.squads = {s["id"]: s for s in raw["squads"]}
            self.rounds = raw["rounds"]
            self.last_refresh = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self.ready = True

        log(f"Ready: {len(self.players)} players, {len(self.squads)} teams, {len(self.rounds)} rounds")

    # --- lookups ---
    def player(self, pid):
        return self.players.get(pid, {})

    def player_name(self, pid):
        p = self.players.get(pid)
        if not p:
            return f"#{pid}"
        if p.get("knownName"):
            return p["knownName"]
        return f"{p.get('firstName', '') or ''} {p.get('lastName', '') or ''}".strip() or f"#{pid}"

    def squad_abbr(self, sid):
        s = self.squads.get(sid)
        return s["abbr"] if s else "?"

    def squad_name(self, sid):
        s = self.squads.get(sid)
        return s["name"] if s else f"#{sid}"

    def player_round_points(self, pid, round_id):
        stats = (self.players.get(pid) or {}).get("stats") or {}
        rp = stats.get("roundPoints") or []
        if isinstance(rp, dict):
            for k in (str(round_id), int(round_id)):
                if k in rp:
                    val = rp[k]
                    if isinstance(val, dict):
                        for sub_k in ("points", "value", "total", "p"):
                            if sub_k in val:
                                return val[sub_k]
                    return val
            return None

        for item in rp:
            if isinstance(item, dict):
                rid = item.get("roundId", item.get("round", item.get("id")))
                if rid == round_id:
                    for k in ("points", "value", "total", "p"):
                        if k in item:
                            return item[k]
        nums = [x for x in rp if isinstance(x, (int, float))]
        if rp and len(nums) == len(rp):
            idx = round_id - 1
            if 0 <= idx < len(rp):
                return rp[idx]
        return None

    def player_total_points(self, pid):
        return ((self.players.get(pid) or {}).get("stats") or {}).get("totalPoints")

    def players_by_squad(self, squad_id):
        return [p for p in self.players.values() if p.get("squadId") == squad_id]

    def is_match_hours(self):
        now = datetime.now(timezone.utc)
        for r in self.rounds:
            for match in _get_round_matches(r):
                start = _parse_match_time(match)
                if start and start <= now <= start + timedelta(hours=2):
                    return True
        return False

    def current_round_id(self):
        best = None
        for r in self.rounds:
            st = r.get("status", "")
            if st and st != "scheduled":
                best = r.get("id")
        return best or (self.rounds[0].get("id") if self.rounds else None)


# ---------------------------------------------------------------------------
# League / team helpers
# ---------------------------------------------------------------------------
def fetch_league(league_id, cookie, limit=DEFAULT_LIMIT):
    url = f"{BASE}/api/en/fantasy/ranking/league/{league_id}?limit={limit}"
    data = http_get_json(url, cookie=cookie)
    return (data.get("success") or {}).get("ranks", []) or []


def fetch_team(round_id, user_id, cookie):
    url = f"{BASE}/api/en/fantasy/team/history/{round_id}/{user_id}"
    data = http_get_json(url, cookie=cookie)
    return data.get("success")


def fetch_player_stats(pid):
    """Per-player, per-round stat breakdown (public, no auth).

    Returns a list of {"roundId", "points", "stats": {GS, AS, CS, ...}} records —
    the raw event counts behind each round's fantasy total.
    """
    url = f"{BASE}/json/fantasy/player_stats/{pid}.json"
    data = http_get_json(url)
    return data if isinstance(data, list) else []


def get_player_fixtures(squad_id, round_id, store):
    fixtures = []
    if not squad_id:
        return fixtures
    target_round = None
    for r in store.rounds:
        if r.get("id") == round_id:
            target_round = r
            break
    if not target_round:
        return fixtures

    for match in _get_round_matches(target_round):
        home_id, away_id = _get_match_teams(match)
        if home_id == squad_id or away_id == squad_id:
            is_home = (home_id == squad_id)
            opp_id = away_id if is_home else home_id
            status = (match.get("status") or "").lower()
            is_live = status in ('playing', 'live', 'inprogress')
            is_done = status in ('complete', 'completed', 'full_time')
            
            fixtures.append({
                "opp_abbr": store.squad_abbr(opp_id),
                "opp_name": store.squad_name(opp_id),
                "is_home": is_home,
                "status": status,
                "is_live": is_live,
                "is_done": is_done
            })
    return fixtures


def is_booster_active(booster_val, round_id):
    if not booster_val:
        return False
    if isinstance(booster_val, dict):
        return booster_val.get("roundId") == round_id
    try:
        return int(booster_val) == round_id
    except (ValueError, TypeError):
        return False


def build_squad_rows(team, store, round_id):
    captain = team.get("captain")
    vice = team.get("vice")
    lineup = team.get("lineup") or {}
    bench = team.get("bench") or {}
    bench_order = team.get("benchOrder") or []
    twelfth_man = team.get("twelfthMan")
    twelfth_man_pid = None
    if twelfth_man and isinstance(twelfth_man, dict) and twelfth_man.get("roundId") == round_id:
        twelfth_man_pid = twelfth_man.get("playerId")
    rows = []

    def make_row(pid, starter, role=""):
        p = store.player(pid)
        if not role:
            role = "C" if pid == captain else ("V" if pid == vice else "")
        squad_id = p.get("squadId")
        return {
            "pid": pid,
            "pos": p.get("position", "?"),
            "name": store.player_name(pid),
            "country": store.squad_abbr(squad_id),
            "price": p.get("price"),
            "points": store.player_round_points(pid, round_id),
            "total": store.player_total_points(pid),
            "role": role,
            "starter": starter,
            "fixtures": get_player_fixtures(squad_id, round_id, store),
        }

    for pos in POS_ORDER:
        for pid in lineup.get(pos, []):
            rows.append(make_row(pid, True))

    if twelfth_man_pid:
        rows.append(make_row(twelfth_man_pid, True, role="12th"))

    bench_ids = list(bench_order) if bench_order else []
    if not bench_ids:
        for pos in POS_ORDER:
            bench_ids.extend(bench.get(pos, []))
    for pid in bench_ids:
        rows.append(make_row(pid, False))

    return rows


def build_owner_map_parallel(managers, cookie, round_id):
    """Fetch all teams in parallel and build player_id → [{"name": manager_name, "is_captain": bool, "is_vice": bool, "is_twelfth_man": bool}]."""
    owner_map = {}
    lock = threading.Lock()

    def fetch_one(mgr):
        uid = mgr.get("userId")
        name = mgr.get("userName", f"User{uid}")
        try:
            team = fetch_team(round_id, uid, cookie)
            if not team:
                return
            lineup = team.get("lineup") or {}
            bench = team.get("bench") or {}
            captain = team.get("captain")
            vice = team.get("vice")
            twelfth_man = team.get("twelfthMan")
            twelfth_man_pid = None
            if twelfth_man and isinstance(twelfth_man, dict) and twelfth_man.get("roundId") == round_id:
                twelfth_man_pid = twelfth_man.get("playerId")
            
            pids = []
            for pos in POS_ORDER:
                pids.extend(lineup.get(pos, []))
                pids.extend(bench.get(pos, []))
            with lock:
                for pid in pids:
                    owner_map.setdefault(pid, []).append({
                        "name": name,
                        "is_captain": pid == captain,
                        "is_vice": pid == vice,
                        "is_twelfth_man": False
                    })
                if twelfth_man_pid:
                    owner_map.setdefault(twelfth_man_pid, []).append({
                        "name": name,
                        "is_captain": False,
                        "is_vice": False,
                        "is_twelfth_man": True
                    })
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch_one, m) for m in managers]
        for f in as_completed(futs):
            pass  # errors already swallowed inside fetch_one

    return owner_map


# ---------------------------------------------------------------------------
# Server-side cache for owner maps (avoids re-fetching 20 teams every request)
# ---------------------------------------------------------------------------
_owner_cache: dict = {}  # (league_id, round_id) → (ts, owner_map)
_owner_cache_lock = threading.Lock()


def get_owner_map(league_id, round_id, cookie, managers):
    key = (league_id, round_id)
    ttl = 60 if store.is_match_hours() else 300
    now = time.time()
    with _owner_cache_lock:
        entry = _owner_cache.get(key)
        if entry and now - entry[0] < ttl:
            return entry[1]
    om = build_owner_map_parallel(managers, cookie, round_id)
    with _owner_cache_lock:
        _owner_cache[key] = (now, om)
    return om


# ---------------------------------------------------------------------------
# Disk-backed cache for per-player stat breakdowns (for the Awards tab)
#
# Player stats only change while a match is live, and finished rounds are
# immutable — so we persist them to fifa_cache/player_stats.json and re-fetch a
# player only when (a) a match is currently live, (b) a new round has completed
# since we cached them, or (c) the cached copy was taken mid-match. Outside
# match hours this means zero API calls after the first load.
# ---------------------------------------------------------------------------
_STATS_CACHE_FILE = os.path.join(CACHE_DIR, "player_stats.json")
_stats_cache: dict = {}  # pid → {"ts", "played", "live", "data"}
_stats_cache_lock = threading.Lock()
_stats_cache_loaded = False


def _finished_round_count():
    return sum(1 for r in store.rounds if r.get("status") == "complete")


def _load_stats_cache():
    global _stats_cache_loaded
    with _stats_cache_lock:
        if _stats_cache_loaded:
            return
        _stats_cache_loaded = True
        try:
            with open(_STATS_CACHE_FILE, encoding="utf-8") as f:
                disk = json.load(f)
            for k, v in disk.items():
                _stats_cache[int(k)] = v
        except Exception:
            pass


def _save_stats_cache():
    with _stats_cache_lock:
        snapshot = {str(pid): v for pid, v in _stats_cache.items()}
    tmp = _STATS_CACHE_FILE + ".tmp"
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _STATS_CACHE_FILE)
    except Exception:
        pass


def clear_stats_cache():
    with _stats_cache_lock:
        _stats_cache.clear()
    _save_stats_cache()


def _stats_fresh(entry, finished):
    if not entry or entry.get("data") is None:
        return False
    # Finished-round stats are immutable, so a cached copy stays fresh until a
    # new round completes (or a manual refresh clears the cache).
    return entry.get("finished", -1) >= finished


def get_player_stats_map(pids):
    """pid → [round records], fetched in parallel, cached in memory and on disk.

    One fetch per player covers every round, so callers can slice by round_id.
    Since the Awards tab only ever asks about completed rounds, a player is
    re-fetched only when a new round has finished since we last cached them.
    """
    _load_stats_cache()
    finished = _finished_round_count()
    now = time.time()
    result, missing = {}, []
    with _stats_cache_lock:
        for pid in pids:
            entry = _stats_cache.get(pid)
            if _stats_fresh(entry, finished):
                result[pid] = entry["data"]
            else:
                missing.append(pid)

    def _one(pid):
        try:
            return pid, fetch_player_stats(pid)
        except Exception:
            return pid, None

    if missing:
        with ThreadPoolExecutor(max_workers=16) as ex:
            for pid, data in ex.map(_one, missing):
                if data is not None:
                    with _stats_cache_lock:
                        _stats_cache[pid] = {"ts": now, "finished": finished, "data": data}
                    result[pid] = data
        _save_stats_cache()
    return result


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
store = DataStore()
_cookie_cache = None


def update_env_file(key, value):
    env_file = os.path.join(HERE, ".env")
    lines = []
    found = False
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
    new_line = f'{key}="{value}"\n'
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = new_line
            found = True
            break
            
    if not found:
        lines.append(new_line)
        
    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _load_env():
    env_file = os.path.join(HERE, ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key not in os.environ:
                            os.environ[key] = val
        except Exception as e:
            print(f"[env] Failed to load .env: {e}", flush=True)

    # Auto-generate SECRET_API_KEY if not present
    if not os.environ.get("SECRET_API_KEY"):
        try:
            import secrets
            new_key = secrets.token_hex(16)
            update_env_file("SECRET_API_KEY", new_key)
            os.environ["SECRET_API_KEY"] = new_key
            print(f"[env] Auto-generated secure SECRET_API_KEY: {new_key}", flush=True)
        except Exception as e:
            print(f"[env] Failed to auto-generate SECRET_API_KEY: {e}", flush=True)


def _is_cookie_expired(cookie_str):
    if not cookie_str:
        return True
        
    # First, try to get the expiration time from the FIFA_COOKIE_EXPIRES env variable
    _load_env()
    expires_str = os.environ.get("FIFA_COOKIE_EXPIRES")
    if expires_str:
        try:
            expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if now >= (expires_dt - timedelta(minutes=2)):
                return True
            return False
        except Exception as e:
            print(f"[auth] Error parsing FIFA_COOKIE_EXPIRES: {e}", flush=True)

    # Fallback to older mechanism where fp.user was inside the cookie string
    fp_user_val = None
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("fp.user="):
            fp_user_val = part.split("=", 1)[1]
            break
            
    if not fp_user_val:
        return False
        
    try:
        import urllib.parse
        fp_user = json.loads(urllib.parse.unquote(fp_user_val))
        expires_str = fp_user.get("expires")
        if expires_str:
            expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if now >= (expires_dt - timedelta(minutes=2)):
                return True
    except Exception as e:
        print(f"[auth] Error parsing cookie expiry: {e}", flush=True)
        
    return False


def _load_cookie():
    global _cookie_cache
    if _cookie_cache:
        if _is_cookie_expired(_cookie_cache):
            print("[auth] Cached cookie is expired. Clearing cache.", flush=True)
            _cookie_cache = None
        else:
            return _cookie_cache

    _load_env()
    env_cookie = os.environ.get("FIFA_COOKIE") or os.environ.get("COOKIE")
    if env_cookie:
        if _is_cookie_expired(env_cookie):
            print("[auth] Warning: Env cookie is expired. Please update it via the web UI.", flush=True)
        
        _cookie_cache = env_cookie.strip()
        return _cookie_cache

    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, encoding="utf-8") as f:
                _cookie_cache = f.read().strip() or None
        except Exception:
            pass
    return _cookie_cache






def _background_refresh():
    """Refresh static data every 60 s while any match is live."""
    while True:
        time.sleep(60)
        if store.ready and store.is_match_hours():
            try:
                store.load_static(force=True)
                print("[auto-refresh] Refreshed during match hours", flush=True)
                # invalidate owner cache so next request re-fetches teams
                with _owner_cache_lock:
                    _owner_cache.clear()
            except Exception as e:
                print(f"[auto-refresh] Error: {e}", flush=True)


def _init():
    _load_cookie()
    try:
        store.load_static(log=lambda m: print(f"[init] {m}", flush=True))
    except Exception as e:
        print(f"[init] Failed: {e}", flush=True)
    threading.Thread(target=_background_refresh, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    _load_env()
    secret_key = os.environ.get("SECRET_API_KEY", "")
    return render_template("index.html", api_key=secret_key)


@app.route("/api/status")
def api_status():
    return jsonify({
        "ready": store.ready,
        "last_refresh": store.last_refresh,
        "players": len(store.players),
        "squads": len(store.squads),
        "rounds": len(store.rounds),
        "is_match_hours": store.is_match_hours() if store.ready else False,
        "default_league_id": DEFAULT_LEAGUE_ID,
        "current_round_id": store.current_round_id() if store.ready else None,
    })





@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _cookie_cache
    _cookie_cache = None  # Reset cookie cache so it reloads from environment/.env
    def do():
        try:
            store.load_static(force=True)
            with _owner_cache_lock:
                _owner_cache.clear()
            clear_stats_cache()
        except Exception as e:
            print(f"[manual-refresh] {e}", flush=True)
    threading.Thread(target=do, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update-cookie", methods=["POST"])
@app.route("/api/update-cookie/", methods=["POST"])
def api_update_cookie():
    _load_env()
    secret_key = os.environ.get("SECRET_API_KEY")
    if not secret_key:
        return jsonify({"error": "SECRET_API_KEY is not configured on the server."}), 500
        
    req_key = request.headers.get("X-Api-Key")
    if not req_key or req_key != secret_key:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json or {}
    new_cookie = data.get("cookie")
    expires = data.get("expires")
    if not new_cookie:
        return jsonify({"error": "Missing cookie in request body"}), 400
        
    try:
        update_env_file("FIFA_COOKIE", new_cookie)
        
        # Try to extract expires from fp.user in the cookie if not provided
        if not expires:
            for part in new_cookie.split(";"):
                part = part.strip()
                if part.startswith("fp.user="):
                    try:
                        import urllib.parse
                        fp_user_val = part.split("=", 1)[1]
                        fp_user = json.loads(urllib.parse.unquote(fp_user_val))
                        expires = fp_user.get("expires")
                    except Exception:
                        pass
                    break

        if expires:
            update_env_file("FIFA_COOKIE_EXPIRES", expires)
            os.environ["FIFA_COOKIE_EXPIRES"] = expires
        else:
            if "FIFA_COOKIE_EXPIRES" in os.environ:
                del os.environ["FIFA_COOKIE_EXPIRES"]
            update_env_file("FIFA_COOKIE_EXPIRES", "")
        
        # Clear server caches so it picks up the new cookie instantly
        global _cookie_cache
        _cookie_cache = new_cookie.strip()
        os.environ["FIFA_COOKIE"] = new_cookie.strip()
        if "COOKIE" in os.environ:
            del os.environ["COOKIE"]
            
        print("[auth] Cookie successfully updated via API sync endpoint.", flush=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Failed to update cookie: {str(e)}"}), 500


@app.route("/api/rounds")
def api_rounds():
    out = []
    for r in store.rounds:
        out.append({
            "id": r.get("id"),
            "name": r.get("name") or f"GW {r.get('id')}",
            "status": r.get("status", ""),
        })
    return jsonify(out)


@app.route("/api/league")
def api_league():
    cookie = _load_cookie()
    if not cookie:
        return jsonify({"error": "No cookie configured on the server."}), 400
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id = int(request.args["round_id"])
    except (KeyError, ValueError):
        return jsonify({"error": "round_id required"}), 400

    try:
        managers = fetch_league(league_id, cookie)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    teams = {}
    def _fetch(mgr):
        uid = mgr.get("userId")
        try:
            team = fetch_team(round_id, uid, cookie)
            if team:
                twelfth_man = team.get("twelfthMan")
                if not is_booster_active(twelfth_man, round_id):
                    twelfth_man = None

                wildcard = team.get("wildCard")
                if not is_booster_active(wildcard, round_id):
                    wildcard = None

                max_captain = team.get("maxCaptain") or team.get("maxCaptainBooster")
                if not is_booster_active(max_captain, round_id):
                    max_captain = None

                teams[uid] = {
                    "roundPoints": team.get("roundPoints"),
                    "overallPoints": team.get("overallPoints"),
                    "value": team.get("value"),
                    "captain": team.get("captain"),
                    "vice": team.get("vice"),
                    "twelfthMan": twelfth_man,
                    "wildCard": wildcard,
                    "maxCaptain": max_captain,
                    "rows": build_squad_rows(team, store, round_id),
                }
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_fetch, managers))

    return jsonify({"managers": managers, "teams": teams, "round_id": round_id})


@app.route("/api/match-view")
def api_match_view():
    cookie = _load_cookie()
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id_str = request.args.get("round_id", "")
        round_id = int(round_id_str) if round_id_str else None
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    # Pick rounds to show
    if round_id:
        target_rounds = [r for r in store.rounds if r.get("id") == round_id]
    else:
        target_rounds = store.rounds

    # Collect all matches
    all_matches = []
    for r in target_rounds:
        rid = r.get("id")
        for match in _get_round_matches(r):
            home_id, away_id = _get_match_teams(match)
            start = _parse_match_time(match)
            all_matches.append({
                "round_id": rid,
                "match_id": match.get("id"),
                "home_id": home_id,
                "away_id": away_id,
                "start_utc": start,
                "status": match.get("status", ""),
                "score_home": match.get("homeScore") or (match.get("score") or {}).get("home"),
                "score_away": match.get("awayScore") or (match.get("score") or {}).get("away"),
            })

    all_matches.sort(key=lambda m: m["start_utc"] or datetime.min.replace(tzinfo=timezone.utc))

    # Build owner map if cookie available
    owner_map = {}
    if cookie:
        try:
            managers = fetch_league(league_id, cookie)
            use_rid = round_id or store.current_round_id() or 1
            owner_map = get_owner_map(league_id, use_rid, cookie, managers)
        except Exception:
            pass

    # Group by Turkish date → hour → matches
    groups: dict = {}
    for m in all_matches:
        start = m["start_utc"]
        if start:
            start_tr = start.astimezone(TURKEY_TZ)
            date_key = start_tr.strftime("%Y-%m-%d")
            date_label = start_tr.strftime("%A, %d %B %Y")
            hour_key = start_tr.strftime("%H:%M")
        else:
            date_key = "unknown"
            date_label = "Unknown date"
            hour_key = "--:--"

        home_id = m["home_id"]
        away_id = m["away_id"]

        # Only players owned by at least one manager
        all_players = []
        for squad_id in [home_id, away_id]:
            if not squad_id:
                continue
            for p in store.players_by_squad(squad_id):
                pid = p.get("id")
                owners = owner_map.get(pid, [])
                if not owners:
                    continue
                all_players.append({
                    "pid": pid,
                    "name": store.player_name(pid),
                    "pos": p.get("position", "?"),
                    "country": store.squad_abbr(squad_id),
                    "squadId": squad_id,
                    "price": p.get("price"),
                    "gwPts": store.player_round_points(pid, m["round_id"]) if m["round_id"] else None,
                    "totalPts": store.player_total_points(pid),
                    "owners": sorted(owners, key=lambda x: x["name"]),
                })

        if not all_players:
            continue

        pos_rank = {pos: i for i, pos in enumerate(POS_ORDER)}
        all_players.sort(key=lambda pl: (
            0 if pl["squadId"] == home_id else 1,
            pos_rank.get(pl["pos"], 99),
        ))

        match_data = {
            "match_id": m["match_id"],
            "round_id": m["round_id"],
            "home_id": home_id,
            "away_id": away_id,
            "home_abbr": store.squad_abbr(home_id),
            "away_abbr": store.squad_abbr(away_id),
            "home_name": store.squad_name(home_id),
            "away_name": store.squad_name(away_id),
            "score_home": m["score_home"],
            "score_away": m["score_away"],
            "status": m["status"],
            "players": all_players,
        }

        if date_key not in groups:
            groups[date_key] = {"label": date_label, "hours": {}}
        if hour_key not in groups[date_key]["hours"]:
            groups[date_key]["hours"][hour_key] = []
        groups[date_key]["hours"][hour_key].append(match_data)

    result = []
    for date_key in sorted(groups.keys()):
        g = groups[date_key]
        hours_list = [
            {"time": hk, "matches": g["hours"][hk]}
            for hk in sorted(g["hours"].keys())
        ]
        result.append({"date": date_key, "label": g["label"], "hours": hours_list})

    now_tr = datetime.now(TURKEY_TZ)
    return jsonify({
        "groups": result,
        "is_match_hours": store.is_match_hours(),
        "now_tr": now_tr.strftime("%H:%M TRT"),
        "has_owners": bool(owner_map),
    })


@app.route("/api/most-picked")
def api_most_picked():
    cookie = _load_cookie()
    if not cookie:
        return jsonify({"error": "No cookie configured on the server."}), 400
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id_str = request.args.get("round_id", "")
        round_id = int(round_id_str) if round_id_str else None
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    use_rid = round_id or store.current_round_id() or 1
    try:
        managers = fetch_league(league_id, cookie)
        owner_map = get_owner_map(league_id, use_rid, cookie, managers)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    total_managers = len(managers)
    players = []
    for pid, owners in owner_map.items():
        p = store.player(pid)
        players.append({
            "pid": pid,
            "name": store.player_name(pid),
            "pos": p.get("position", "?"),
            "country": store.squad_abbr(p.get("squadId")),
            "price": p.get("price"),
            "gwPts": store.player_round_points(pid, use_rid),
            "totalPts": store.player_total_points(pid),
            "count": len(owners),
            "owners": sorted(owners, key=lambda x: x["name"]),
        })

    # Most picked first; tie-break by total points, then name
    players.sort(key=lambda x: (-x["count"], -(x["totalPts"] or 0), x["name"].lower()))

    return jsonify({
        "players": players,
        "total_managers": total_managers,
        "round_id": use_rid,
    })


def rarity_score(k, n):
    """Surprisal-based rarity for a player owned by k of n managers.

    Scaled so a solo pick (k=1) is worth 10 points and a fully-owned
    'template' player (k=n) is worth 0. Rewards uniqueness nonlinearly.
    """
    if n <= 1 or k <= 0 or k >= n:
        return 0.0 if k >= n else 10.0
    return 10.0 * math.log2(n / k) / math.log2(n)


@app.route("/api/marjinal")
def api_marjinal():
    """'En Marjinal' — rank managers by how off-template (unique) their squad is.

    round_id="overall" aggregates rarity across every played gameweek; a numeric
    (or empty → current) round_id scores just that gameweek.
    """
    cookie = _load_cookie()
    if not cookie:
        return jsonify({"error": "No cookie configured on the server."}), 400

    round_id_str = request.args.get("round_id", "")
    overall = (round_id_str == "overall")
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id = None if overall or not round_id_str else int(round_id_str)
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    if overall:
        round_ids = [r.get("id") for r in store.rounds
                     if r.get("status") and r.get("status") != "scheduled"]
        round_ids = round_ids or [store.current_round_id() or 1]
    else:
        round_ids = [round_id or store.current_round_id() or 1]

    try:
        managers = fetch_league(league_id, cookie)
        owner_maps = {rid: get_owner_map(league_id, rid, cookie, managers) for rid in round_ids}
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    n = len(managers)

    # Seed every manager so all appear even with a 0 score.
    mgr_stats = {}
    for m in managers:
        name = m.get("userName", f"User{m.get('userId')}")
        mgr_stats[name] = {"name": name, "score": 0.0, "solo": 0, "players": {}}

    def _stat(name):
        st = mgr_stats.get(name)
        if st is None:
            st = mgr_stats.setdefault(
                name, {"name": name, "score": 0.0, "solo": 0, "players": {}})
        return st

    for rid, owner_map in owner_maps.items():
        for pid, owners in owner_map.items():
            k = len(owners)
            rarity = rarity_score(k, n)
            gw = store.player_round_points(pid, rid)
            for o in owners:
                st = _stat(o["name"])
                st["score"] += rarity
                if k == 1:
                    st["solo"] += 1
                p = store.player(pid)
                pl = st["players"].get(pid)
                if pl is None:
                    pl = st["players"][pid] = {
                        "pid": pid,
                        "name": store.player_name(pid),
                        "pos": p.get("position", "?"),
                        "country": store.squad_abbr(p.get("squadId")),
                        "owners": k,      # best (rarest) ownership seen while held
                        "rarity": 0.0,    # total rarity contributed
                        "rounds": 0,      # gameweeks held
                        "gwPts": 0,
                        "is_captain": False,
                        "is_twelfth_man": False,
                    }
                pl["rarity"] += rarity
                pl["rounds"] += 1
                pl["owners"] = min(pl["owners"], k)
                if gw is not None:
                    pl["gwPts"] += gw
                pl["is_captain"] = pl["is_captain"] or bool(o.get("is_captain"))
                pl["is_twelfth_man"] = pl["is_twelfth_man"] or bool(o.get("is_twelfth_man"))

    ranking = []
    for st in mgr_stats.values():
        breakdown = list(st["players"].values())
        for pl in breakdown:
            pl["rarity"] = round(pl["rarity"], 2)
        breakdown.sort(key=lambda x: (-x["rarity"], x["name"].lower()))
        ranking.append({
            "name": st["name"],
            "score": round(st["score"], 1),
            "solo": st["solo"],
            "breakdown": breakdown,
        })
    # Most marjinal first; tie-break by solo picks, then name.
    ranking.sort(key=lambda x: (-x["score"], -x["solo"], x["name"].lower()))

    return jsonify({
        "ranking": ranking,
        "total_managers": n,
        "overall": overall,
        "rounds_counted": len(round_ids),
        "round_id": "overall" if overall else round_ids[0],
    })


@app.route("/api/twins")
def api_twins():
    """'Twin Detector' — rank manager pairs by how similar their squads are.

    For each unordered pair we measure squad overlap via the Jaccard index
    (|A ∩ B| / |A ∪ B|). round_id="overall" sums intersections/unions across
    every played gameweek; a numeric (or empty → current) round_id compares a
    single gameweek. Also counts how often the pair captained the same player.
    """
    cookie = _load_cookie()
    if not cookie:
        return jsonify({"error": "No cookie configured on the server."}), 400

    round_id_str = request.args.get("round_id", "")
    overall = (round_id_str == "overall")
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id = None if overall or not round_id_str else int(round_id_str)
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    if overall:
        round_ids = [r.get("id") for r in store.rounds
                     if r.get("status") and r.get("status") != "scheduled"]
        round_ids = round_ids or [store.current_round_id() or 1]
    else:
        round_ids = [round_id or store.current_round_id() or 1]

    try:
        managers = fetch_league(league_id, cookie)
        owner_maps = {rid: get_owner_map(league_id, rid, cookie, managers) for rid in round_ids}
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    names = sorted({m.get("userName", f"User{m.get('userId')}") for m in managers})

    # Per-round: manager → set of owned player ids, and manager → captain pid.
    round_sets = {}   # rid → {name: set(pid)}
    round_caps = {}   # rid → {name: captain_pid}
    for rid, owner_map in owner_maps.items():
        sets, caps = {}, {}
        for pid, owners in owner_map.items():
            for o in owners:
                nm = o["name"]
                sets.setdefault(nm, set()).add(pid)
                if o.get("is_captain"):
                    caps[nm] = pid
        round_sets[rid] = sets
        round_caps[rid] = caps

    def _pos_key(pos):
        return POS_ORDER.index(pos) if pos in POS_ORDER else len(POS_ORDER)

    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            total_shared = total_union = rounds_counted = same_captain = 0
            shared_rounds = {}   # pid → number of gameweeks shared
            for rid in round_ids:
                sa = round_sets[rid].get(a)
                sb = round_sets[rid].get(b)
                if not sa or not sb:
                    continue
                rounds_counted += 1
                inter = sa & sb
                total_shared += len(inter)
                total_union += len(sa | sb)
                for pid in inter:
                    shared_rounds[pid] = shared_rounds.get(pid, 0) + 1
                ca, cb = round_caps[rid].get(a), round_caps[rid].get(b)
                if ca is not None and ca == cb:
                    same_captain += 1
            if rounds_counted == 0:
                continue

            shared_players = []
            for pid, cnt in shared_rounds.items():
                p = store.player(pid)
                shared_players.append({
                    "name": store.player_name(pid),
                    "pos": p.get("position", "?"),
                    "country": store.squad_abbr(p.get("squadId")),
                    "rounds": cnt,
                })
            shared_players.sort(key=lambda x: (-x["rounds"], _pos_key(x["pos"]), x["name"].lower()))

            pairs.append({
                "a": a,
                "b": b,
                "shared": round(total_shared / rounds_counted, 1),
                "jaccard": round(total_shared / total_union * 100, 1) if total_union else 0.0,
                "same_captain": same_captain,
                "rounds_counted": rounds_counted,
                "players": shared_players,
            })

    # Most alike first; tie-break by raw shared count, then names.
    pairs.sort(key=lambda x: (-x["jaccard"], -x["shared"], x["a"].lower(), x["b"].lower()))

    return jsonify({
        "pairs": pairs,
        "total_managers": len(managers),
        "overall": overall,
        "rounds_counted": len(round_ids),
        "round_id": "overall" if overall else round_ids[0],
    })


# FIFA WC Fantasy 2026 scoring — validated to reproduce the feed's per-round
# totals exactly (409/409 player-rounds). Position-dependent goal/CS values.
_GOAL_PTS = {"GK": 9, "DEF": 7, "MID": 6, "FWD": 5}
_CS_PTS = {"GK": 5, "DEF": 5, "MID": 1}  # FWD earn nothing; needs 60+ min


def award_breakdown(pos, s):
    """Split a player's round into award buckets → (points, event_count).

    Only the categories the Awards tab ranks on; mirrors the validated scoring
    rules (clean sheet requires 60+ minutes; keeper saves score 1 per 3).
    """
    out = {}
    mp = s.get("MP", 0) or 0
    gs = s.get("GS", 0) or 0
    if gs and pos in _GOAL_PTS:
        out["goals"] = (gs * _GOAL_PTS[pos], gs)
    cs = s.get("CS", 0) or 0
    if cs and mp >= 60 and pos in _CS_PTS:
        out["clean_sheet"] = (_CS_PTS[pos], 1)
    a = s.get("AS", 0) or 0
    if a:
        out["assists"] = (a * 3, a)
    if pos == "GK":
        sv = s.get("S", 0) or 0
        ps = s.get("PS", 0) or 0
        pts = sv // 3 + ps * 3
        cnt = sv + ps
        if pts or cnt:
            out["saves"] = (pts, cnt)
    pw = s.get("PW", 0) or 0
    if pw:
        out["pens_won"] = (pw * 2, pw)
    sb = s.get("SB", 0) or 0
    if sb:
        out["scouting"] = (sb * 2, sb)
    if pos == "MID":
        t = s.get("T", 0) or 0
        cc = s.get("CC", 0) or 0
        pts = t // 3 + cc // 2
        if t or cc:
            out["workrate"] = (pts, t + cc)
    yc = s.get("YC", 0) or 0
    rc = s.get("RC", 0) or 0
    og = s.get("OG", 0) or 0
    if yc or rc or og:
        out["discipline"] = (-(yc + 2 * rc + 2 * og), yc + rc + og)
    return out


# (key, title, subtitle, unit, emoji, rank_by_count) — evocative titles; the
# subtitle underneath carries the plain explanation.
_AWARDS = [
    ("goals",       "Golden Boot",   "Points earned from goals scored",                     "goals",        "⚽", False),
    ("clean_sheet", "The Great Wall", "Points earned from clean sheets",                    "clean sheets", "🧤", False),
    ("assists",     "The Playmaker", "Points earned from assists",                          "assists",      "🅰️", False),
    ("saves",       "Safe Hands",    "Points from keeper saves & penalty stops",            "saves",        "🧱", False),
    ("pens_won",    "Penalty Winner", "Points earned from winning penalties",               "pens won",     "🎯", False),
    ("scouting",    "The Scout",     "Scouting bonuses from differentials (<5% owned)",     "scout picks",  "🔍", False),
    ("workrate",    "Engine Room",   "Points from midfield tackles & big chances created",  "actions",      "🏃", False),
    ("discipline",  "The Villain",   "Points lost to cards and own goals",                  "cards",        "🟥", True),
]


@app.route("/api/awards")
def api_awards():
    """Per-category 'awards' — attribute each player's category points to the
    managers who owned them (captain ×2). Awards are only computed for *completed*
    gameweeks; round_id='overall' sums across all finished rounds, a numeric
    round_id scores that one (if finished), and empty → latest finished round.
    """
    cookie = _load_cookie()
    if not cookie:
        return jsonify({"error": "No cookie configured on the server."}), 400

    round_id_str = request.args.get("round_id", "")
    overall = (round_id_str == "overall")
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id = None if overall or not round_id_str else int(round_id_str)
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    finished_ids = [r.get("id") for r in store.rounds if r.get("status") == "complete"]

    if overall:
        if not finished_ids:
            return jsonify({"pending": True,
                            "message": "Awards unlock once the first gameweek is complete."})
        round_ids = finished_ids
    else:
        target = round_id if round_id else (finished_ids[-1] if finished_ids else None)
        status = next((r.get("status") for r in store.rounds if r.get("id") == target), None)
        if target is None or status != "complete":
            label = f"GW {target}" if target else "This gameweek"
            return jsonify({"pending": True, "round_id": target,
                            "message": f"{label} awards unlock once the gameweek is complete."})
        round_ids = [target]

    try:
        managers = fetch_league(league_id, cookie)
        owner_maps = {rid: get_owner_map(league_id, rid, cookie, managers) for rid in round_ids}
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    pids = set()
    for om in owner_maps.values():
        pids.update(om.keys())
    stats_map = get_player_stats_map(pids)
    # (pid, rid) → stat dict for O(1) slicing.
    stats_by_round = {}
    for pid, recs in stats_map.items():
        for rec in recs or []:
            stats_by_round[(pid, rec.get("roundId"))] = rec.get("stats") or {}

    names = sorted({m.get("userName", f"User{m.get('userId')}") for m in managers})

    # name → bucket → {"points", "count"} ; name → bucket → pid → player detail
    agg = {nm: {} for nm in names}
    detail = {nm: {} for nm in names}

    def _played(pid, rid):
        return ((stats_by_round.get((pid, rid)) or {}).get("MP", 0) or 0) > 0

    for rid, owner_map in owner_maps.items():
        # Effective double pick per manager: the captain, or the vice-captain if
        # the captain didn't play a minute (mirrors FIFA's vice-captain rule).
        cap_pid, vice_pid = {}, {}
        for pid, owners in owner_map.items():
            for o in owners:
                if o.get("is_captain"):
                    cap_pid[o["name"]] = pid
                if o.get("is_vice"):
                    vice_pid[o["name"]] = pid
        double_pid = {}
        for nm, cpid in cap_pid.items():
            if _played(cpid, rid) or nm not in vice_pid:
                double_pid[nm] = cpid
            else:
                double_pid[nm] = vice_pid[nm]

        for pid, owners in owner_map.items():
            s = stats_by_round.get((pid, rid))
            if not s:
                continue
            pos = store.player(pid).get("position", "?")
            bd = award_breakdown(pos, s)
            if not bd:
                continue
            for o in owners:
                nm = o["name"]
                cap = (double_pid.get(nm) == pid)
                mult = 2 if cap else 1
                for bk, (pts, cnt) in bd.items():
                    a = agg.setdefault(nm, {}).setdefault(bk, {"points": 0.0, "count": 0})
                    a["points"] += pts * mult
                    a["count"] += cnt
                    dbucket = detail.setdefault(nm, {}).setdefault(bk, {})
                    d = dbucket.get(pid)
                    if d is None:
                        d = dbucket[pid] = {
                            "name": store.player_name(pid),
                            "pos": pos,
                            "country": store.squad_abbr(store.player(pid).get("squadId")),
                            "points": 0.0,
                            "count": 0,
                            "cap": False,
                        }
                    d["points"] += pts * mult
                    d["count"] += cnt
                    d["cap"] = d["cap"] or cap

    categories = []
    for key, title, subtitle, unit, emoji, by_count in _AWARDS:
        rows = []
        for nm in names:
            st = agg.get(nm, {}).get(key)
            pts = round(st["points"], 1) if st else 0.0
            cnt = st["count"] if st else 0
            players = list(detail.get(nm, {}).get(key, {}).values())
            for p in players:
                p["points"] = round(p["points"], 1)
            players.sort(key=lambda x: (-abs(x["points"]), -x["count"], x["name"].lower()))
            rows.append({"name": nm, "points": pts, "count": cnt, "players": players[:6]})
        if by_count:  # discipline: most offences first
            rows.sort(key=lambda r: (-r["count"], r["points"], r["name"].lower()))
        else:
            rows.sort(key=lambda r: (-r["points"], -r["count"], r["name"].lower()))
        categories.append({
            "key": key, "title": title, "subtitle": subtitle,
            "unit": unit, "emoji": emoji, "by_count": by_count,
            "ranking": rows,
        })

    return jsonify({
        "categories": categories,
        "total_managers": len(managers),
        "overall": overall,
        "rounds_counted": len(round_ids),
        "round_id": "overall" if overall else round_ids[0],
    })


@app.route("/api/optimize-squad")
def api_optimize_squad():
    cookie = _load_cookie()
    try:
        league_id = int(request.args.get("league_id", DEFAULT_LEAGUE_ID))
        round_id_str = request.args.get("round_id", "")
        budget = float(request.args.get("budget", 100.0))
        max_per_team = int(request.args.get("max_per_team", 3))
        pool = request.args.get("pool", "managers")
        squad_type = request.args.get("squad_type", "squad")
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    if pool == "managers" and not cookie:
        return jsonify({"error": "No cookie configured on the server. Please refresh cookie or choose All Players pool."}), 400

    current_rid = store.current_round_id() or 1
    if round_id_str == "total":
        use_round_id = None
        candidate_round_id = current_rid
    else:
        try:
            use_round_id = int(round_id_str) if round_id_str else current_rid
            candidate_round_id = use_round_id
        except ValueError:
            use_round_id = current_rid
            candidate_round_id = current_rid

    candidate_pids = None
    owner_map = {}
    if pool == "managers":
        try:
            managers = fetch_league(league_id, cookie)
            owner_map = get_owner_map(league_id, candidate_round_id, cookie, managers)
            candidate_pids = set(owner_map.keys())
        except Exception as e:
            return jsonify({"error": f"Failed to fetch league players: {str(e)}"}), 502
    else:
        if cookie:
            try:
                managers = fetch_league(league_id, cookie)
                owner_map = get_owner_map(league_id, current_rid, cookie, managers)
            except Exception:
                pass

    valid_players = []
    for pid, p in store.players.items():
        if candidate_pids is not None and pid not in candidate_pids:
            continue
        price = p.get("price")
        pos = p.get("position")
        if price is not None and pos in POS_ORDER:
            if use_round_id is None:
                pts = store.player_total_points(pid)
            else:
                pts = store.player_round_points(pid, use_round_id)
            
            pts = float(pts) if pts is not None else 0.0
            
            valid_players.append({
                "pid": pid,
                "name": store.player_name(pid),
                "pos": pos,
                "country": store.squad_abbr(p.get("squadId")),
                "squadId": p.get("squadId"),
                "price": float(price),
                "pts": pts,
                "totalPts": store.player_total_points(pid) or 0,
                "gwPts": store.player_round_points(pid, use_round_id or current_rid) or 0,
                "owners": sorted(owner_map.get(pid, []), key=lambda x: x["name"]),
            })

    if not valid_players:
        return jsonify({"error": "No players available with points data."}), 400

    import numpy as np
    from scipy.optimize import milp, Bounds, LinearConstraint

    n = len(valid_players)
    c = np.array([-p["pts"] for p in valid_players])
    bounds = Bounds(np.zeros(n), np.ones(n))
    integrality = np.ones(n)

    A = []
    lb = []
    ub = []

    # 1. Total count
    total_count = 15 if squad_type == "squad" else 11
    A.append(np.ones(n))
    lb.append(total_count)
    ub.append(total_count)

    # 2. Positions
    if squad_type == "squad":
        pos_limits = {"GK": (2, 2), "DEF": (5, 5), "MID": (5, 5), "FWD": (3, 3)}
    else:
        pos_limits = {"GK": (1, 1), "DEF": (3, 5), "MID": (3, 5), "FWD": (1, 3)}

    for pos, (l_val, u_val) in pos_limits.items():
        row = np.array([1.0 if p["pos"] == pos else 0.0 for p in valid_players])
        A.append(row)
        lb.append(l_val)
        ub.append(u_val)

    # 3. Budget
    row_budget = np.array([p["price"] for p in valid_players])
    A.append(row_budget)
    lb.append(0.0)
    ub.append(budget)

    # 4. Max per team
    squad_ids = set(p["squadId"] for p in valid_players if p["squadId"] is not None)
    for sid in squad_ids:
        row_team = np.array([1.0 if p["squadId"] == sid else 0.0 for p in valid_players])
        A.append(row_team)
        lb.append(0)
        ub.append(max_per_team)

    A = np.vstack(A)
    lb = np.array(lb)
    ub = np.array(ub)

    constraints = LinearConstraint(A, lb, ub)

    res = milp(c, bounds=bounds, constraints=constraints, integrality=integrality)

    if res.success:
        selected_indices = np.where(res.x > 0.5)[0]
        selected_players = [valid_players[i] for i in selected_indices]
        
        pos_rank = {pos: i for i, pos in enumerate(POS_ORDER)}
        selected_players.sort(key=lambda x: (pos_rank.get(x["pos"], 99), -x["pts"], x["name"]))
        
        total_points = sum(p["pts"] for p in selected_players)
        total_cost = sum(p["price"] for p in selected_players)

        return jsonify({
            "success": True,
            "players": selected_players,
            "total_points": round(total_points, 1),
            "total_cost": round(total_cost, 2),
            "squad_type": squad_type,
            "round_id": round_id_str,
            "pool": pool,
            "budget": budget,
            "max_per_team": max_per_team,
        })
    else:
        return jsonify({
            "success": False,
            "error": f"Optimization failed: {res.message}. Try increasing the budget, choosing a different squad type, or increasing max players per country."
        })


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _init()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)