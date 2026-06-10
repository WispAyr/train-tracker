#!/usr/bin/env python3
"""
Train Tracker Proxy Server
Proxies JourneyCheck ScotRail data and serves the frontend.

Surfaces:
  /            presenter view (Now Ayrshire Radio on-air travel)
  /diagram     time-distance diagram (old homepage)
  /board       departure board
  /map         live transport map
  /signalman   signalman panel
"""

import re
import html as html_mod
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

app = FastAPI(title="Train Tracker Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOURNEYCHECK_BASE = "https://www.journeycheck.com/scotrail/route"
JOURNEYCHECK_SUMMARY = "https://www.journeycheck.com/scotrail/route?action=summary"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
LONDON = ZoneInfo("Europe/London")

# Station name mapping from JourneyCheck names to our CRS codes
STATION_NAME_TO_CRS = {
    "ayr": "AYR",
    "newton-on-ayr": "NOA",
    "prestwick town": "PTW",
    "prestwick int. airport": "PRA",
    "prestwick international airport": "PRA",
    "troon": "TRN",
    "barassie": "BSS",
    "irvine": "IRV",
    "kilwinning": "KWN",
    "dalry": "DLY",
    "lochwinnoch": "LHW",
    "milliken park": "MIK",
    "johnstone": "JHN",
    "paisley gilmour street": "PYG",
    "glasgow central": "GLC",
}

# ---------------------------------------------------------------------------
# Presenter view: Ayrshire corridors + disruption notices
# ---------------------------------------------------------------------------

CORRIDORS = [
    {
        "id": "ayr-glasgow",
        "name": "Ayr — Glasgow Central",
        "short": "Ayr–Glasgow",
        "from": "AYR", "to": "GLC",
        "out_label": "To Glasgow", "in_label": "To Ayr",
    },
    {
        "id": "kilmarnock-glasgow",
        "name": "Kilmarnock — Glasgow Central",
        "short": "Kilmarnock–Glasgow",
        "from": "KMK", "to": "GLC",
        "out_label": "To Glasgow", "in_label": "To Kilmarnock",
    },
    {
        "id": "largs-glasgow",
        "name": "Largs / Ardrossan — Glasgow Central",
        "short": "Largs–Glasgow",
        "from": "LAR", "to": "GLC",
        "out_label": "To Glasgow", "in_label": "To Largs",
    },
    {
        "id": "ayr-girvan",
        "name": "Ayr — Girvan / Stranraer",
        "short": "Ayr–Girvan",
        "from": "AYR", "to": "GIR",
        "out_label": "To Girvan", "in_label": "To Ayr",
    },
]

# Stations used to decide whether a ScotRail-wide notice is relevant to
# Ayrshire listeners. Deliberately excludes Glasgow Central (too broad).
AYRSHIRE_KEYWORDS = [
    "ayr", "newton-on-ayr", "prestwick", "troon", "barassie", "irvine",
    "kilwinning", "dalry", "glengarnock", "lochwinnoch", "milliken park",
    "johnstone", "paisley gilmour", "largs", "fairlie", "west kilbride",
    "ardrossan", "saltcoats", "stevenston", "kilmarnock", "stewarton",
    "dunlop", "kilmaurs", "maybole", "girvan", "barrhill", "stranraer",
    "auchinleck", "new cumnock", "kirkconnel", "sanquhar",
]

INCIDENT_SECTIONS = {
    "LU": "Line update",
    "TC": "Cancellation",
    "OTA": "Service update",
    "SUS": "Station update",
    "EWU": "Engineering works",
    "LD": "Line update",
    "LA": "Line update",
}

# in-process cache: key -> (epoch, payload)
_cache: dict = {}
CACHE_TTL = 90  # seconds


def normalize_station(name: str) -> str:
    """Convert station name to CRS code."""
    name = name.strip().rstrip("\xa0").strip().lower()
    return STATION_NAME_TO_CRS.get(name, name.upper()[:3])


def parse_calling_pattern(html_block: str) -> list:
    """Parse a calling pattern block into station stops."""
    stops = []
    # Match each calling pattern row
    rows = re.findall(
        r'<tr class="callingPatternRow">(.*?)</tr>',
        html_block,
        re.DOTALL
    )
    for row in rows:
        # Extract scheduled time - look for HH:MM pattern with Dep./Arr.
        time_match = re.search(r'(\d{2}:\d{2})\s*(?:&nbsp;)?(Dep\.|Arr\.)', row)
        scheduled = time_match.group(1) if time_match else None
        dep_arr = time_match.group(2) if time_match else None

        # Extract all td elements in order
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        # tds[0] = scheduled time cell (with img spacer + time)
        # tds[1] = expected status (On Time, or HH:MM)
        # tds[2] = station name
        # tds[3] = platform

        expected = tds[1].strip() if len(tds) > 1 else "On Time"
        station_raw = tds[2].strip() if len(tds) > 2 else None
        platform = tds[3].strip() if len(tds) > 3 else ""

        # Clean station name
        if station_raw:
            station_raw = station_raw.replace('&nbsp;', '').strip()

        if scheduled and station_raw:
            crs = normalize_station(station_raw)
            stops.append({
                "station": station_raw,
                "crs": crs,
                "scheduled": scheduled,
                "expected": expected,
                "type": "departure" if dep_arr == "Dep." else "arrival",
                "platform": platform,
            })
    return stops


def parse_departures_html(html: str) -> dict:
    """Parse JourneyCheck HTML into structured departure/arrival data."""
    result = {"departures": [], "arrivals": []}

    # Split into departure and arrival sections
    dep_section = ""
    arr_section = ""

    dep_match = re.search(
        r'id="departureBoardBlock"(.*?)(?=id="arrivalBoardBlock"|$)',
        html, re.DOTALL
    )
    arr_match = re.search(
        r'id="arrivalBoardBlock"(.*?)(?=id="co2Block"|$)',
        html, re.DOTALL
    )

    if dep_match:
        dep_section = dep_match.group(1)
    if arr_match:
        arr_section = arr_match.group(1)

    # Parse departures
    for section, key in [(dep_section, "departures"), (arr_section, "arrivals")]:
        if not section:
            continue

        prefix = "Dep" if key == "departures" else "Arr"

        # Find service rows by the showHideUpadtes onclick pattern
        svc_pattern = (
            r'onclick="showHideUpadtes\(\'callingPattern'
            + prefix
            + r'(\d+)\'[^"]*"[^>]*>.*?</tr>'
        )
        service_rows = list(re.finditer(svc_pattern, section, re.DOTALL))

        for svc_match in service_rows:
            row_html = svc_match.group(0)
            idx = svc_match.group(1)

            # Extract scheduled time
            sched_match = re.search(r'headers="scheduled' + prefix + r'">\s*(\d{2}:\d{2})', row_html)
            scheduled = sched_match.group(1).strip() if sched_match else None
            if not scheduled:
                continue

            # Extract expected
            exp_match = re.search(r'headers="expected' + prefix + r'">(.*?)</td>', row_html, re.DOTALL)
            expected = exp_match.group(1).strip() if exp_match else "On Time"

            # Extract destination/origin
            header_name = "destination" + prefix if key == "departures" else "origin" + prefix
            dest_match = re.search(
                r'headers="' + header_name + r'[^"]*"[^>]*>\s*(.*?)\s*</td>',
                row_html, re.DOTALL
            )
            destination = dest_match.group(1).strip() if dest_match else ""

            # Extract platform
            plat_match = re.search(
                r'class="platformCell"[^>]*>(.*?)</td>',
                row_html, re.DOTALL
            )
            platform = plat_match.group(1).strip().replace("&nbsp;", "").strip() if plat_match else ""

            # Get calling pattern
            cp_pattern = (
                r'id="callingPattern' + prefix + idx
                + r'".*?<table.*?>(.*?)</table>'
            )
            cp_match = re.search(cp_pattern, section, re.DOTALL)
            calling_points = []
            if cp_match:
                calling_points = parse_calling_pattern(cp_match.group(1))

            # Calculate delay
            delay_mins = 0
            if expected != "On Time" and expected != "Cancelled":
                exp_time = re.search(r'(\d{2}:\d{2})', expected)
                if exp_time:
                    sh, sm = map(int, scheduled.split(":"))
                    eh, em = map(int, exp_time.group(1).split(":"))
                    delay_mins = (eh * 60 + em) - (sh * 60 + sm)

            service = {
                "scheduled": scheduled,
                "expected": expected,
                "destination": destination,
                "platform": platform,
                "delay_mins": delay_mins,
                "cancelled": "cancelled" in expected.lower() if expected else False,
                "calling_points": calling_points,
            }
            result[key].append(service)

    return result


def _strip_tags(s: str) -> str:
    s = re.sub(r'<script.*?</script>', ' ', s, flags=re.DOTALL)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html_mod.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def parse_incidents_html(html: str) -> list:
    """Parse the JourneyCheck all-incidents summary page into notices."""
    incidents = []
    # Split page into mainSection chunks: id="LU", "TC", "OTA", ...
    parts = re.split(r'<div class="mainSection" id="([A-Z]+)">', html)
    # parts = [pre, id1, body1, id2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        section_id = parts[i]
        body = parts[i + 1]
        category = INCIDENT_SECTIONS.get(section_id)
        if not category:
            continue  # catering / formations / unknown — not on-air material
        blocks = re.findall(
            r'<div class="primaryStyle updateTitle">(.*?)'
            r'<div class="primaryStyle updateBodyStart">(.*?)'
            r'(?=<div class="primaryStyle updateTitle">|<div class="mainSection"|$)',
            body, re.DOTALL
        )
        for title_html, body_html in blocks:
            title = _strip_tags(title_html)
            title = re.sub(r'^(expand|collapse)\s*', '', title).strip()
            updated_match = re.search(r'Last Updated\s*:\s*([^<]+)<', body_html)
            updated = updated_match.group(1).strip() if updated_match else ""
            # Body text: everything before the calling-pattern table / Last Updated
            text_part = re.split(r'<div class="primaryStyle messageRecieved"', body_html)[0]
            text = _strip_tags(text_part)
            # Drop accessibility/helpline boilerplate — not on-air material
            text = re.split(r'Additional Information\s*:', text)[0].strip()
            reason_match = re.search(r'[Tt]his is (?:due to|because of) ([^.]+)\.', text)
            reason = reason_match.group(1).strip() if reason_match else ""
            # Stations mentioned anywhere in the block (incl. calling pattern)
            haystack = (title + " " + _strip_tags(body_html)).lower()
            matched = [k for k in AYRSHIRE_KEYWORDS if k in haystack]
            if not title:
                continue
            incidents.append({
                "category": category,
                "title": title,
                "body": text[:600],
                "reason": reason,
                "updated": updated,
                "ayrshire_stations": matched,
                "relevant": bool(matched),
            })
    return incidents


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _hhmm_to_mins(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _summarise_direction(deps: list, direction_label: str) -> dict:
    """Reduce a departure list to problems + next services for one direction."""
    problems = []
    next_services = []
    max_delay = 0
    cancelled = 0
    for svc in deps:
        exp = (svc.get("expected") or "").lower()
        item = {
            "scheduled": svc["scheduled"],
            "destination": svc["destination"],
            "expected": svc["expected"],
            "platform": svc.get("platform", ""),
            "delay_mins": svc.get("delay_mins", 0),
            "direction": direction_label,
        }
        if svc.get("cancelled"):
            cancelled += 1
            problems.append({**item, "kind": "cancelled"})
        elif "delayed" in exp:
            problems.append({**item, "kind": "delayed"})
            max_delay = max(max_delay, svc.get("delay_mins", 0) or 0)
        elif svc.get("delay_mins", 0) >= 5:
            problems.append({**item, "kind": "delayed"})
            max_delay = max(max_delay, svc["delay_mins"])
        if len(next_services) < 4:
            next_services.append(item)
    return {
        "problems": problems,
        "next": next_services,
        "max_delay": max_delay,
        "cancelled": cancelled,
        "total": len(deps),
    }


def _corridor_status(max_delay: int, cancelled: int, has_unknown_delay: bool) -> tuple:
    if cancelled > 0:
        return ("cancellations", "Cancellations")
    if max_delay >= 15:
        return ("major", "Major delays")
    if max_delay >= 5 or has_unknown_delay:
        return ("minor", "Minor delays")
    return ("good", "Good service")


def build_script(corridors: list, incidents: list, now: datetime) -> dict:
    """Generate plain-English on-air bulletin text."""
    time_str = now.strftime("%H:%M")
    trouble = [c for c in corridors if c["status"] != "good"]
    lines = []

    if not trouble:
        quick = "Good service on all Ayrshire rail lines right now."
        lines.append(
            f"Here's your Ayrshire rail check at {time_str} — and it's good news: "
            "trains are running on time between Ayr and Glasgow, on the Kilmarnock "
            "line, the Largs and Ardrossan coast line, and south to Girvan."
        )
    else:
        names = ", ".join(c["short"] for c in trouble)
        quick = f"Rail disruption on the {names} line{'s' if len(trouble) > 1 else ''}."
        lines.append(f"Here's your Ayrshire rail check at {time_str}.")
        for c in corridors:
            if c["status"] == "good":
                continue
            probs = c["problems"]
            cancelled = [p for p in probs if p["kind"] == "cancelled"]
            delayed = [p for p in probs if p["kind"] == "delayed"]
            seg = []
            if cancelled:
                examples = ", ".join(
                    f"the {p['scheduled']} {p['direction'].lower()} ({p['destination']})"
                    for p in cancelled[:3]
                )
                seg.append(
                    f"On the {c['short']} line, "
                    f"{len(cancelled)} service{'s are' if len(cancelled) > 1 else ' is'} cancelled — {examples}."
                )
            if delayed:
                worst = max((p.get("delay_mins") or 0) for p in delayed)
                if worst > 0:
                    seg.append(
                        f"Delays of up to {worst} minutes on the {c['short']} line."
                    )
                else:
                    seg.append(f"Some services on the {c['short']} line are running late.")
            lines.append(" ".join(seg))
        good = [c for c in corridors if c["status"] == "good"]
        if good:
            lines.append(
                "Elsewhere, trains are running normally on the "
                + " and ".join(c["short"] for c in good) + " line"
                + ("s." if len(good) > 1 else ".")
            )

    # Fold in relevant ScotRail notices (reasons are gold on air)
    for inc in incidents:
        if inc.get("relevant") and inc.get("reason"):
            lines.append(f"ScotRail says: {inc['title']} — due to {inc['reason']}.")

    lines.append("That's your trains — more travel on the way.")
    return {"quick": quick, "full": " ".join(lines)}


async def build_presenter_payload() -> dict:
    now = datetime.now(LONDON)
    async with httpx.AsyncClient(timeout=20.0) as client:
        tasks = []
        for c in CORRIDORS:
            tasks.append(_fetch(client, f"{JOURNEYCHECK_BASE}?from={c['from']}&to={c['to']}"))
            tasks.append(_fetch(client, f"{JOURNEYCHECK_BASE}?from={c['to']}&to={c['from']}"))
        tasks.append(_fetch(client, JOURNEYCHECK_SUMMARY))
        pages = await asyncio.gather(*tasks)

    incidents = parse_incidents_html(pages[-1]) if pages[-1] else []
    relevant_incidents = [i for i in incidents if i["relevant"]]

    corridors = []
    for i, c in enumerate(CORRIDORS):
        out_html, in_html = pages[i * 2], pages[i * 2 + 1]
        out_data = parse_departures_html(out_html) if out_html else {"departures": []}
        in_data = parse_departures_html(in_html) if in_html else {"departures": []}
        out_sum = _summarise_direction(out_data.get("departures", []), c["out_label"])
        in_sum = _summarise_direction(in_data.get("departures", []), c["in_label"])
        problems = out_sum["problems"] + in_sum["problems"]
        max_delay = max(out_sum["max_delay"], in_sum["max_delay"])
        cancelled = out_sum["cancelled"] + in_sum["cancelled"]
        has_unknown = any(
            p["kind"] == "delayed" and not p.get("delay_mins") for p in problems
        )
        status, status_label = _corridor_status(max_delay, cancelled, has_unknown)
        data_ok = bool(out_sum["total"] or in_sum["total"])
        corridors.append({
            **{k: c[k] for k in ("id", "name", "short", "out_label", "in_label")},
            "status": status if data_ok else "unknown",
            "status_label": status_label if data_ok else "No data",
            "problems": problems,
            "next_out": out_sum["next"],
            "next_in": in_sum["next"],
            "max_delay": max_delay,
            "cancelled_count": cancelled,
        })

    live = [c for c in corridors if c["status"] != "unknown"]
    if any(c["status"] == "cancellations" for c in live):
        overall = ("cancellations", "Cancellations on Ayrshire lines")
    elif any(c["status"] == "major" for c in live):
        overall = ("major", "Major delays on Ayrshire lines")
    elif any(c["status"] == "minor" for c in live):
        overall = ("minor", "Minor delays on Ayrshire lines")
    elif live:
        overall = ("good", "Good service on all Ayrshire lines")
    else:
        overall = ("unknown", "Live data unavailable")

    script = build_script(live, relevant_incidents, now)

    return {
        "generated_at": now.isoformat(),
        "generated_hhmm": now.strftime("%H:%M"),
        "overall": {"status": overall[0], "headline": overall[1]},
        "corridors": corridors,
        "incidents": relevant_incidents,
        "incidents_network": [i for i in incidents if not i["relevant"]][:10],
        "script": script,
    }


@app.get("/api/presenter")
async def presenter_api():
    """Aggregated Ayrshire corridor status + on-air script (cached)."""
    cached = _cache.get("presenter")
    if cached and time.time() - cached[0] < CACHE_TTL:
        return JSONResponse(cached[1])
    try:
        payload = await build_presenter_payload()
        _cache["presenter"] = (time.time(), payload)
        return JSONResponse(payload)
    except Exception as e:
        if cached:
            return JSONResponse({**cached[1], "stale": True})
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/departures/{from_stn}/{to_stn}")
async def get_departures(from_stn: str, to_stn: str):
    """Fetch and parse departures from JourneyCheck."""
    url = f"{JOURNEYCHECK_BASE}?from={from_stn.upper()}&to={to_stn.upper()}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
            if r.status_code != 200:
                return JSONResponse(
                    {"error": f"JourneyCheck returned {r.status_code}"},
                    status_code=502
                )
            data = parse_departures_html(r.text)
            return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/all")
async def get_all_services():
    """Fetch both northbound and southbound services."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Northbound: Ayr to Glasgow
            nb_url = f"{JOURNEYCHECK_BASE}?from=AYR&to=GLC"
            # Southbound: Glasgow to Ayr
            sb_url = f"{JOURNEYCHECK_BASE}?from=GLC&to=AYR"

            nb_resp, sb_resp = await asyncio.gather(
                client.get(nb_url, headers={"User-Agent": USER_AGENT}),
                client.get(sb_url, headers={"User-Agent": USER_AGENT}),
            )

            northbound = parse_departures_html(nb_resp.text) if nb_resp.status_code == 200 else {"departures": [], "arrivals": []}
            southbound = parse_departures_html(sb_resp.text) if sb_resp.status_code == 200 else {"departures": [], "arrivals": []}

            return JSONResponse({
                "northbound": northbound,
                "southbound": southbound,
            })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _serve(name: str) -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / name).read_text())


@app.get("/")
async def index():
    """Presenter view — the new default."""
    return _serve("presenter.html")


@app.get("/diagram")
async def diagram():
    """Time-distance diagram (the old homepage)."""
    return _serve("index.html")


@app.get("/signalman")
async def signalman():
    """Serve the signalman panel page."""
    return _serve("signalman.html")


@app.get("/board")
async def board():
    """Serve the departure board page."""
    return _serve("board.html")


@app.get("/map")
async def map_page():
    """Serve the live transport map page."""
    return _serve("map.html")


@app.get("/api/buses")
async def buses():
    """Proxy bustimes.org vehicle locations for Ayrshire area."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://bustimes.org/vehicles.json"
                "?ymax=55.9&xmax=-4.1&ymin=55.3&xmin=-4.9",
                headers={"User-Agent": USER_AGENT},
            )
            if r.status_code != 200:
                return JSONResponse([], status_code=200)
            return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/nuro")
async def nuro_feed():
    """Nuro integration — live transport summary for coherence layer."""
    try:
        bus_data = []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://bustimes.org/vehicles.json"
                    "?ymax=55.9&xmax=-4.1&ymin=55.3&xmin=-4.9",
                    headers={"User-Agent": USER_AGENT},
                )
                if r.status_code == 200:
                    bus_data = r.json()
        except Exception:
            pass

        # Derive operator counts from vehicle URL prefixes
        op_map = {"stws": "Stagecoach West", "scnh": "Stagecoach", "mega": "Megabus",
                  "embr": "Ember", "scfi": "First Scotland", "mcgl": "McGills"}
        operators = {}
        for b in bus_data:
            vurl = (b.get("vehicle") or {}).get("url", "")
            prefix = vurl.split("/vehicles/")[-1].split("-")[0] if "/vehicles/" in vurl else "unknown"
            op = op_map.get(prefix, "Other")
            operators[op] = operators.get(op, 0) + 1

        routes = set()
        for b in bus_data:
            svc = b.get("service") or {}
            ln = svc.get("line_name")
            if ln:
                routes.add(ln)

        from datetime import timezone as _tz
        now_iso = datetime.now(_tz.utc).isoformat()
        bus_count = len(bus_data)
        route_count = len(routes)

        streams = [
            {
                "id": "bus_count",
                "type": "scalar",
                "value": bus_count,
                "label": "Live Buses",
                "render": "stat",
                "severity": "ok" if bus_count > 0 else "warning",
                "updated": now_iso,
            },
            {
                "id": "route_count",
                "type": "scalar",
                "value": route_count,
                "label": "Active Routes",
                "render": "stat",
                "updated": now_iso,
            },
            {
                "id": "operators",
                "type": "breakdown",
                "label": "Operators",
                "render": "bar",
                "data": operators,
                "updated": now_iso,
            },
            {
                "id": "coverage",
                "type": "geospatial",
                "label": "Coverage Area",
                "render": "text",
                "value": "Ayrshire (55.3-55.9N, 4.1-4.9W)",
                "updated": now_iso,
            },
        ]

        return {
            # v2 envelope
            "service": "train-tracker",
            "version": "2.0",
            "label": "Ayrshire Transport",
            "icon": "🚌",
            "url": "https://trains.wispayr.online",
            "streams": streams,
            # Backward-compatible flat fields
            "bus_count": bus_count,
            "train_count": 0,  # TODO: add when ScotRail scrape is reliable
            "operators": operators,
            "active_routes": sorted(routes),
            "route_count": route_count,
            "coverage": "Ayrshire (55.3-55.9N, 4.1-4.9W)",
            "data_sources": ["bustimes.org", "scotrail (scrape)"],
        }
    except Exception as e:
        return {"service": "train-tracker", "error": str(e)}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(__import__("os").environ.get("PORT","3974")))
