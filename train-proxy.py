#!/usr/bin/env python3
"""
Train Tracker Proxy Server
Proxies JourneyCheck ScotRail data and serves the frontend.
Port: 3970
"""

import re
import asyncio
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

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


@app.get("/")
async def index():
    """Serve the main HTML page."""
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/signalman")
async def signalman():
    """Serve the signalman panel page."""
    html_path = Path(__file__).parent / "signalman.html"
    return HTMLResponse(html_path.read_text())


@app.get("/board")
async def board():
    """Serve the departure board page."""
    html_path = Path(__file__).parent / "board.html"
    return HTMLResponse(html_path.read_text())


@app.get("/map")
async def map_page():
    """Serve the live transport map page."""
    html_path = Path(__file__).parent / "map.html"
    return HTMLResponse(html_path.read_text())


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3970)
