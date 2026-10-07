"""WSDOT plugin for FiestaBoard.

Integrates with Washington State Department of Transportation APIs.
Initial feature: Washington State Ferries (schedules, vessels, sailing space, alerts).
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from src.devices import BoardContext
from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

# WSF REST API base URLs (HTTPS)
WSF_SCHEDULE_BASE = "https://www.wsdot.wa.gov/Ferries/API/Schedule/rest"
WSF_TERMINALS_BASE = "https://www.wsdot.wa.gov/Ferries/API/Terminals/rest"
WSF_VESSELS_BASE = "https://www.wsdot.wa.gov/Ferries/API/Vessels/rest"

# Request timeout seconds
REQUEST_TIMEOUT = 15

# Well-known WSF route IDs for display names (can be extended)
ROUTE_NAMES: Dict[int, str] = {
    1: "Seattle-Bainbridge",
    2: "Seattle-Bremerton",
    3: "Fauntleroy-Vashon-Southworth",
    4: "Point Defiance-Tahlequah",
    5: "Anacortes-San Juan",
    6: "Anacortes-Sidney BC",
    7: "Mukilteo-Clinton",
    8: "Port Townsend-Keystone",
    9: "Edmonds-Kingston",
}

# Board geometry defaults. self.board is None outside a board-scoped render
# (unit tests, legacy callers); the documented contract is to assume a
# Flagship (22x6) rather than crash. These are the ONLY board-dimension
# literals in this file -- everything on a layout path derives from _dims().
DEFAULT_BOARD_ROWS = 6
DEFAULT_BOARD_COLS = 22

# Widest board FiestaBoard supports: an 8-wide note array (8 * NOTE_COLS).
# Used only to size the manifest's max_lengths honestly -- never as a layout
# literal.
MAX_BOARD_COLS = 120

# Header line describing formatted fields (Route, Time, Spots) for display
# above data. Natural length is 16 tiles; _format_headers() shrinks it for a
# narrower board and never pads it wider than that.
HEADERS_TEXT = "Route Time Spots"

# Alert text/headlines come from the live WSDOT API with no length contract,
# so it is truncated at the boundary to match what the manifest declares
# rather than leaving it unbounded.
MAX_ALERT_TEXT_LENGTH = 22


def _dims(board: Optional[BoardContext]) -> "tuple[int, int]":
    """Return (rows, cols) for *board*, defaulting to a Flagship when unbound."""
    if board is None:
        return DEFAULT_BOARD_ROWS, DEFAULT_BOARD_COLS
    return board.rows, board.cols


def _format_headers(cols: int) -> str:
    """Column header line, shrunk to fit a narrower board, never padded wider."""
    return HEADERS_TEXT[:cols]


# Short abbreviations for board display. Max ~8 chars for route.
ROUTE_ABBREVS: Dict[int, str] = {
    1: "Sea-Bain",
    2: "Sea-Brem",
    3: "Fau-Vash",
    4: "PtDef-Tah",
    5: "Anac-SJ",
    6: "Anac-Sid",
    7: "Muk-Clin",
    8: "PT-Keyst",
    9: "Edm-King",
}

# Route ID -> (terminal_id_a, terminal_id_b) for fallback when primary terminal has no space data
ROUTE_TERMINALS: Dict[int, tuple] = {
    1: (7, 3),    # Seattle-Bainbridge
    2: (7, 4),    # Seattle-Bremerton
    3: (9, 22),   # Fauntleroy-Vashon-Southworth (Fauntleroy, Vashon)
    4: (16, 21),  # Point Defiance-Tahlequah
    7: (14, 5),   # Mukilteo-Clinton
    8: (17, 11),  # Port Townsend-Keystone (Coupeville)
    9: (8, 12),   # Edmonds-Kingston
}

def _get(obj: Dict, *keys: str, default: Any = None) -> Any:
    """Get value from dict trying multiple key names (PascalCase / camelCase)."""
    for key in keys:
        if key in obj:
            return obj[key]
        # Try lowercase first letter
        alt = key[:1].lower() + key[1:] if key else key
        if alt in obj:
            return obj[alt]
    return default


def _parse_time(s: Any) -> str:
    """Parse API time to HH:MM string. Handles .NET /Date(ms-offset)/ and ISO."""
    if s is None:
        return ""
    if isinstance(s, str):
        # .NET JSON: "/Date(1770212100000-0800)/"
        import re
        m = re.match(r"/Date\((\d+)([+-]\d{4})?\)/", s.strip())
        if m:
            try:
                ms = int(m.group(1))
                # ms is UTC; offset e.g. -0800 means local = UTC - 8 hours (Pacific)
                offset_str = (m.group(2) or "+0000").strip()
                sign = -1 if offset_str[0] == "-" else 1
                hours = int(offset_str[1:3]) * sign
                minutes = int(offset_str[3:5]) * sign
                utc = datetime.utcfromtimestamp(ms / 1000.0).replace(tzinfo=timezone.utc)
                tz = timezone(timedelta(hours=hours, minutes=minutes))
                local = utc.astimezone(tz)
                return local.strftime("%H:%M")
            except (ValueError, OverflowError):
                pass
        # ISO or "HH:MM"
        if "T" in s:
            s = s.split("T")[1][:5]
        return s[:5] if len(s) >= 5 else str(s)
    return str(s)


def _format_route_line(
    route_id: int,
    scheduled_time: str,
    spots_remaining: str,
    max_len: int,
) -> str:
    """Build one abbreviated line for the board (route + time + spots)."""
    route_abbrev = ROUTE_ABBREVS.get(route_id, ROUTE_NAMES.get(route_id, f"R{route_id}")[:8])
    route_abbrev = route_abbrev[:8]
    time_str = (scheduled_time or "--").strip()[:5]
    parts = [route_abbrev, time_str or "--"]
    parts.append(spots_remaining.strip()[:3] if spots_remaining else "--")
    line = " ".join(parts)
    out = line[:max_len]
    # Defensive: if we somehow only have the route, append time placeholder
    if out.strip() == route_abbrev:
        out = f"{route_abbrev} --"[:max_len]
    return out


class WsdotPlugin(PluginBase):
    """WSDOT plugin: Washington State Ferries schedules, vessels, and alerts."""

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        # Keyed by board geometry (see _cache_key_for_board) -- a Flagship's
        # frame must never be served to a Note or a note_array's render.
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._vessel_names: Dict[int, str] = {}
        self._sailing_space: Dict[int, Any] = {}
        self._wait_times: Dict[int, Any] = {}

    @property
    def plugin_id(self) -> str:
        return "wsdot"

    @staticmethod
    def _cache_key_for_board(board: Optional[BoardContext]) -> str:
        """Cache key for *board*'s shape and display.

        Mirrors ``PluginBase._cache_key``. Flagship and Note have fixed sizes,
        so their device_type is a sufficient key. Every other family varies in
        size under one device_type -- note arrays, and LED/TV boards, which
        are all "panel" -- so the dimensions are folded in: otherwise a 16x10
        Pixoo and a 22x9 TV panel share one entry, and a board whose grid
        changes at runtime (a larger text size) is served output laid out for
        its old size. Two boards of one size can still draw differently
        (split-flap vs LED), so the display's key is appended when core
        provides one; ``getattr`` keeps this working on cores whose
        BoardContext has no ``display``.
        """
        if board is None:
            return "_default"
        if board.device_type in ("flagship", "note"):
            key = board.device_type
        else:
            key = f"{board.device_type}:{board.cols}x{board.rows}"
        display = getattr(board, "display", None)
        display_key = getattr(display, "key", None)
        return f"{key}|{display_key}" if display_key else key

    def _get_access_code(self) -> Optional[str]:
        code = self.config.get("api_access_code")
        if not code:
            code = os.getenv("WSDOT_API_ACCESS_CODE")
        return code or None

    def _get(self, base: str, path: str, params: Optional[Dict[str, str]] = None) -> Optional[Any]:
        """GET JSON from WSF API."""
        code = self._get_access_code()
        if not code:
            return None
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        p = dict(params or {})
        p["apiaccesscode"] = code
        headers = {"Accept": "application/json"}
        try:
            r = requests.get(url, params=p, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("WSF API request failed %s: %s", url, e)
            return None

    def _fetch_schedule_today(self, route_id: int) -> Optional[Dict[str, Any]]:
        """Fetch today's schedule for a route. OnlyRemainingTimes=false for full day."""
        path = f"scheduletoday/{route_id}/false"
        return self._get(WSF_SCHEDULE_BASE, path)

    def _fetch_vessel_names(self) -> Dict[int, str]:
        """Fetch vessel ID -> name map."""
        if self._vessel_names:
            return self._vessel_names
        data = self._get(WSF_VESSELS_BASE, "vesselbasics")
        if not data:
            return {}
        # Response may be list or dict with list
        items = data if isinstance(data, list) else _get(data, "VesselBasics", "vesselbasics") or data
        if not isinstance(items, list):
            items = [items]
        for item in items:
            vid = _get(item, "VesselID", "vesselId")
            name = _get(item, "VesselName", "vesselName") or _get(item, "Abbrev", "abbrev")
            if vid is not None and name:
                self._vessel_names[int(vid)] = str(name)
        return self._vessel_names

    def _fetch_terminal_sailing_space(self) -> None:
        """Fetch sailing space (car spots) per terminal."""
        data = self._get(WSF_TERMINALS_BASE, "terminalsailingspace")
        if not data:
            return
        items = data if isinstance(data, list) else _get(data, "TerminalSailingSpaces", "terminalSailingSpaces") or []
        if not isinstance(items, list):
            items = [items] if items else []
        self._sailing_space = {}
        for item in items:
            tid = _get(item, "TerminalID", "terminalId")
            if tid is not None:
                self._sailing_space[int(tid)] = item

    def _get_drive_up_space(
        self,
        terminal_id: int,
        departure_date_str: Any,
        vessel_id: Any,
        route_id: Optional[int] = None,
    ) -> str:
        """Get drive-up space count for a sailing from terminalsailingspace data.
        Match by terminal and vessel; use exact departure time if found, else closest by timestamp.
        If no space found for this terminal and route_id is known, try the route's other terminal.
        """
        terminal_id = int(terminal_id)
        result = self._get_drive_up_space_for_terminal(terminal_id, departure_date_str, vessel_id)
        if result:
            return result
        if route_id is not None:
            pair = ROUTE_TERMINALS.get(route_id)
            if pair:
                other = pair[1] if pair[0] == terminal_id else pair[0]
                if other != terminal_id:
                    result = self._get_drive_up_space_for_terminal(
                        other, departure_date_str, vessel_id
                    )
                    if result:
                        return result
        return ""

    def _get_drive_up_space_for_terminal(
        self, terminal_id: int, departure_date_str: Any, vessel_id: Any
    ) -> str:
        """Resolve drive-up space for a single terminal from terminalsailingspace data."""
        terminal_id = int(terminal_id)
        if terminal_id not in self._sailing_space:
            return ""
        term = self._sailing_space[terminal_id]
        departing = _get(term, "DepartingSpaces", "departingSpaces") or []
        if not isinstance(departing, list):
            departing = [departing]
        try:
            vid = int(vessel_id) if vessel_id is not None else None
        except (TypeError, ValueError):
            vid = None
        dep_str = str(departure_date_str).strip() if departure_date_str is not None else ""

        def _ms_from_date(s: Any) -> Optional[int]:
            if s is None:
                return None
            m = re.match(r"/Date\((\d+)([+-]\d{4})?\)/", str(s).strip())
            return int(m.group(1)) if m else None

        def _item_vessel_id(it: Dict[str, Any]) -> Optional[int]:
            raw = _get(it, "VesselID", "vesselId")
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def _extract_count(it: Dict[str, Any]) -> Optional[str]:
            spaces = _get(it, "SpaceForArrivalTerminals", "spaceForArrivalTerminals") or []
            if not isinstance(spaces, list):
                spaces = [spaces] if spaces else []
            for sp in spaces:
                count = _get(sp, "DriveUpSpaceCount", "driveUpSpaceCount")
                if count is not None:
                    return str(count)[:3]
            count = _get(it, "DriveUpSpaceCount", "driveUpSpaceCount")
            if count is not None:
                return str(count)[:3]
            return None

        target_ms = _ms_from_date(departure_date_str)
        best_count: Optional[str] = None
        best_diff: Optional[float] = None

        for item in departing:
            item_vid = _item_vessel_id(item)
            if vid is not None and item_vid is not None and item_vid != vid:
                continue
            if vid is not None and item_vid is None:
                continue
            dep = _get(item, "Departure", "departure")
            count_str = _extract_count(item)
            if str(dep).strip() == dep_str:
                if count_str is not None:
                    return count_str
            if target_ms is not None and count_str is not None:
                item_ms = _ms_from_date(dep)
                if item_ms is not None:
                    diff = abs(item_ms - target_ms)
                    if best_diff is None or diff < best_diff:
                        best_diff = diff
                        best_count = count_str

        return best_count or ""

    def _fetch_terminal_wait_times(self) -> None:
        """Fetch wait times per terminal."""
        data = self._get(WSF_TERMINALS_BASE, "terminalwaittimes")
        if not data:
            return
        items = data if isinstance(data, list) else _get(data, "TerminalWaitTimes", "terminalWaitTimes") or []
        if not isinstance(items, list):
            items = [items] if items else []
        self._wait_times = {}
        for item in items:
            tid = _get(item, "TerminalID", "terminalId")
            if tid is not None:
                self._wait_times[int(tid)] = item

    def _fetch_alerts(self) -> List[Dict[str, Any]]:
        """Fetch ferry alerts."""
        data = self._get(WSF_SCHEDULE_BASE, "alerts")
        if not data:
            return []
        items = data if isinstance(data, list) else _get(data, "Alerts", "alerts") or []
        if not isinstance(items, list):
            items = [items] if items else []
        out = []
        for item in items[:10]:
            headline = _get(item, "Headline", "headline") or _get(item, "AlertFullTitle", "alertFullTitle") or "Alert"
            body = _get(item, "AlertFullDescription", "alertFullDescription") or _get(item, "Description", "description") or ""
            out.append({
                "headline": str(headline)[:MAX_ALERT_TEXT_LENGTH],
                "alert_text": (str(body) if body else str(headline))[:MAX_ALERT_TEXT_LENGTH],
            })
        return out

    def _parse_schedule_response(self, raw: Any, route_id: int) -> Dict[str, Any]:
        """Parse schedule API response into departures_ab and departures_ba with vessel names and spots."""
        vessels = self._fetch_vessel_names()
        departures_ab: List[Dict[str, Any]] = []
        departures_ba: List[Dict[str, Any]] = []

        # API may return array of sailings or wrapped object.
        # Real WSF API returns { "TerminalCombos": [ { "DepartingTerminalID", "Times": [ {...}, ... ] } ] }
        sailings: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            sailings = raw
        else:
            combos = _get(raw, "TerminalCombos", "terminalCombos")
            if isinstance(combos, list):
                for combo in combos:
                    times = _get(combo, "Times", "times")
                    departing_tid = _get(combo, "DepartingTerminalID", "departingTerminalId")
                    if isinstance(times, list):
                        for t in times:
                            s_copy = dict(t)
                            if departing_tid is not None:
                                s_copy["_departing_terminal_id"] = int(departing_tid)
                            sailings.append(s_copy)
            if not sailings:
                sailings = (
                    _get(raw, "Schedule", "schedule")
                    or _get(raw, "Sailings", "sailings")
                    or _get(raw, "Departures", "departures")
                    or []
                )
            if not isinstance(sailings, list):
                sailings = [sailings] if sailings else []

        for s in sailings[:20]:
            dep_time = _get(
                s,
                "DepartingTime", "departingTime",
                "DepartureTime", "departureTime",
                "LeavingTime", "leavingTime",
                "Time", "time",
                "SailingTime", "sailingTime",
            )
            vessel_id = _get(s, "VesselID", "vesselId", "VesselId")
            vessel_name = _get(s, "VesselName", "vesselName", "Vessel") or ""
            if not vessel_name and vessel_id is not None:
                vessel_name = vessels.get(int(vessel_id), "") or str(vessel_id)
            scheduled_time = _parse_time(dep_time)
            actual_time = _parse_time(_get(s, "ActualDepartureTime", "actualDepartureTime")) or ""
            # Sailing space: from schedule object or from Terminals API (terminalsailingspace)
            spaces = _get(s, "SpacesLeft", "spacesLeft")
            if spaces is None:
                spaces = _get(s, "VehicleCapacityRemaining", "vehicleCapacityRemaining")
            spots = str(spaces) if spaces is not None else ""
            if not spots and s.get("_departing_terminal_id") is not None:
                spots = self._get_drive_up_space(
                    s["_departing_terminal_id"],
                    dep_time,
                    vessel_id,
                    route_id=route_id,
                )

            dep = {
                "scheduled_time": scheduled_time[:5],
                "actual_time": actual_time[:5] if actual_time else "",
                "vessel_name": vessel_name,
                "spots_remaining": spots[:3],
            }
            direction = _get(s, "Direction", "direction")
            if isinstance(direction, str) and "b" in direction.lower():
                departures_ba.append(dep)
            else:
                departures_ab.append(dep)

        # If no direction in data, put all in departures_ab
        if not departures_ba and departures_ab:
            half = len(departures_ab) // 2
            departures_ba = departures_ab[half:]
            departures_ab = departures_ab[:half]

        route_name = ROUTE_NAMES.get(route_id, f"Route {route_id}")
        wait_min = ""
        if self._wait_times:
            # Wait time is per terminal; use first available
            for wt in self._wait_times.values():
                m = _get(wt, "WaitTimeMinutes", "waitTimeMinutes") or _get(wt, "CurrentWaitTime", "currentWaitTime")
                if m is not None:
                    wait_min = str(m)
                    break

        # Board geometry for this render (self.board is bound by get_data()
        # around fetch_data(), so it reflects whichever board is currently
        # being rendered -- Flagship, Note, or a note_array of any size).
        rows, cols = _dims(self.board)

        # Build abbreviated formatted line for board, sized to the actual
        # board rendering right now rather than a hardcoded Flagship width.
        next_dep = (departures_ab or departures_ba or [{}])[0]
        formatted = _format_route_line(
            route_id=route_id,
            scheduled_time=next_dep.get("scheduled_time") or "",
            spots_remaining=next_dep.get("spots_remaining") or "",
            max_len=cols,
        )

        # Stored departures are not capped at a fixed count -- a taller
        # board reflows into more departure rows (see _build_formatted_lines),
        # so the cap scales with board.rows instead of staying fixed at 6.
        dep_cap = max(rows, 6)

        return {
            "route_id": route_id,
            "route_name": route_name,
            "formatted": formatted,
            "headers": _format_headers(cols),
            "departures_ab": departures_ab[:dep_cap],
            "departures_ba": departures_ba[:dep_cap],
            "wait_time_minutes": wait_min,
            "alerts": [],
        }

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        if not config.get("api_access_code") and not os.getenv("WSDOT_API_ACCESS_CODE"):
            errors.append("WSDOT API access code is required (free at https://www.wsdot.wa.gov/traffic/api/)")
        routes = config.get("routes", [])
        if not routes:
            errors.append("At least one ferry route is required")
        for i, r in enumerate(routes):
            if not isinstance(r, dict) or r.get("route_id") is None:
                errors.append(f"Route {i + 1} must have a route_id")
        return errors

    def fetch_data(self) -> PluginResult:
        code = self._get_access_code()
        if not code:
            return PluginResult(
                available=False,
                error="WSDOT API access code not configured. Get a free code at https://www.wsdot.wa.gov/traffic/api/"
            )

        routes_config = self.config.get("routes", [])[:4]
        if not routes_config:
            return PluginResult(available=False, error="No ferry routes configured")

        try:
            self._fetch_vessel_names()
            self._fetch_terminal_sailing_space()
            self._fetch_terminal_wait_times()
            alerts_list = self._fetch_alerts()
        except Exception as e:
            logger.exception("WSF API error during fetch")
            return PluginResult(available=False, error=str(e))

        _, cols = _dims(self.board)

        routes_data: List[Dict[str, Any]] = []
        for r in routes_config:
            route_id = r.get("route_id")
            if route_id is None:
                continue
            try:
                route_id = int(route_id)
            except (TypeError, ValueError):
                continue
            raw = self._fetch_schedule_today(route_id)
            if raw is None:
                abbrev = ROUTE_ABBREVS.get(route_id, ROUTE_NAMES.get(route_id, f"R{route_id}")[:8])[:8]
                routes_data.append({
                    "route_id": route_id,
                    "route_name": ROUTE_NAMES.get(route_id, f"Route {route_id}"),
                    "formatted": f"{abbrev} No data"[:cols],
                    "headers": _format_headers(cols),
                    "departures_ab": [],
                    "departures_ba": [],
                    "wait_time_minutes": "",
                    "alerts": [],
                })
                continue
            parsed = self._parse_schedule_response(raw, route_id)
            routes_data.append(parsed)

        if not routes_data:
            return PluginResult(
                available=False,
                error="Could not load any ferry route data"
            )

        data: Dict[str, Any] = {
            "route_count": len(routes_data),
            "has_alerts": bool(alerts_list),
            "headers": _format_headers(cols),
            "routes": routes_data,
            "alerts": alerts_list,
        }
        primary = routes_data[0]
        data["formatted"] = primary.get("formatted", "WSF")[:cols]

        lines = self._build_formatted_lines(data, self.board)
        self._cache[self._cache_key_for_board(self.board)] = data
        return PluginResult(available=True, data=data, formatted_lines=lines)

    def _build_formatted_lines(
        self, data: Dict[str, Any], board: Optional[BoardContext] = None
    ) -> List[str]:
        """Build the whole-board display, reflowed to *board*'s geometry.

        One row goes to the title and, when alerts are active, one to the
        alert indicator; everything else is spent on departure lines, taken
        round-robin across the configured routes so a taller board shows
        more sailings per route rather than one route hogging every row.
        """
        rows, cols = _dims(board)
        title = "WSF FERRIES"
        lines: List[str] = [title[:cols].center(cols)]

        has_alerts = bool(data.get("has_alerts"))
        budget = max(rows - 1 - (1 if has_alerts else 0), 0)

        routes = data.get("routes") or []
        # Per route: one line per known departure, built fresh from the
        # departure data at this board's width; a route with no departure
        # detail (e.g. the API had nothing) falls back to its single
        # pre-built summary line.
        per_route_lines: List[List[str]] = []
        for route in routes:
            route_id = route.get("route_id")
            deps = list(route.get("departures_ab") or []) + list(route.get("departures_ba") or [])
            if deps:
                per_route_lines.append([
                    _format_route_line(
                        route_id=route_id,
                        scheduled_time=dep.get("scheduled_time") or "",
                        spots_remaining=dep.get("spots_remaining") or "",
                        max_len=cols,
                    )
                    for dep in deps
                ])
            else:
                per_route_lines.append([(route.get("formatted") or "")[:cols]])

        added = 0
        progressed = True
        indices = [0] * len(per_route_lines)
        while added < budget and progressed:
            progressed = False
            for i, route_lines in enumerate(per_route_lines):
                if added >= budget:
                    break
                if indices[i] < len(route_lines):
                    lines.append(route_lines[indices[i]])
                    indices[i] += 1
                    added += 1
                    progressed = True

        if has_alerts:
            lines.append("Alerts active".ljust(cols)[:cols])

        while len(lines) < rows:
            lines.append("")
        return lines[:rows]

    def get_formatted_display(self) -> Optional[List[str]]:
        key = self._cache_key_for_board(self.board)
        cached = self._cache.get(key)
        if cached is None:
            result = self.fetch_data()
            if not result.available:
                return None
            cached = self._cache.get(key)
        return self._build_formatted_lines(cached or {}, self.board)

    def cleanup(self) -> None:
        self._cache = {}
        self._vessel_names = {}
        self._sailing_space = {}
        self._wait_times = {}
        logger.info("%s cleanup", self.plugin_id)


Plugin = WsdotPlugin
