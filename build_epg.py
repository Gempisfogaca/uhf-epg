#!/usr/bin/env python3
"""
Builds an XMLTV guide with past listings (catchup) from an Xtream Codes server.

- Upcoming listings: from the provider's xmltv.php (keeps icons, categories, etc.)
- Past (and missing) listings: from the get_simple_data_table API, for every channel with an EPG id
- Output: public/epg.xml and public/epg.xml.gz

Standard library only (Python 3.9+). Credentials are read from environment variables:
  XTREAM_HOST  e.g. http://example-server.com:8080
  XTREAM_USER
  XTREAM_PASS
Optional:
  PAST_DAYS (default 7)   days of history to include
  WORKERS   (default 6)   parallel API requests
  FETCH_ALL (default 1)   1 = query API for all channels, 0 = catchup channels only
  OUT_DIR   (default public)
"""
import base64
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

HOST = os.environ["XTREAM_HOST"].rstrip("/")
USER = os.environ["XTREAM_USER"]
PASS = os.environ["XTREAM_PASS"]
PAST_DAYS = int(os.environ.get("PAST_DAYS", "7"))
WORKERS = int(os.environ.get("WORKERS", "6"))
OUT_DIR = os.environ.get("OUT_DIR", "public")
UA = "VLC/3.0.20 LibVLC/3.0.20"


def fetch(url, retries=4, timeout=90):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def api(action, **params):
    q = {"username": USER, "password": PASS, "action": action, **params}
    return json.loads(fetch(f"{HOST}/player_api.php?{urllib.parse.urlencode(q)}"))


def b64(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return s


def ts_to_xmltv(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def xmltv_to_ts(value):
    value = value.strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def main():
    now = int(time.time())
    cutoff = now - PAST_DAYS * 86400

    # 1) Base EPG (today + upcoming)
    print("Downloading xmltv.php...", flush=True)
    q = urllib.parse.urlencode({"username": USER, "password": PASS})
    root = ET.fromstring(fetch(f"{HOST}/xmltv.php?{q}", timeout=180))

    channels = {}       # epg_id -> <channel> element
    programmes = {}     # (epg_id, start_ts) -> <programme> element
    for ch in root.findall("channel"):
        channels[ch.get("id")] = ch
    for p in root.findall("programme"):
        start = xmltv_to_ts(p.get("start", ""))
        if start is not None:
            programmes[(p.get("channel"), start)] = p
    base_count = len(programmes)
    print(f"  {len(channels)} channels, {base_count} programmes in XMLTV", flush=True)

    # 2) Catchup channels (one stream per epg_channel_id)
    print("Fetching channel list...", flush=True)
    streams = api("get_live_streams")
    # FETCH_ALL=1 (default): query the API for every channel with an EPG id, so channels
    # missing from (or sparse in) xmltv.php still get a full guide. Catchup streams are preferred.
    fetch_all = os.environ.get("FETCH_ALL", "1") == "1"
    archive = {}  # epg_id -> (stream_id, name)
    ordered = sorted(streams, key=lambda s: str(s.get("tv_archive", "0")) != "1")
    for s in ordered:
        epg_id = s.get("epg_channel_id")
        is_archive = str(s.get("tv_archive", "0")) == "1"
        if epg_id and (is_archive or fetch_all) and epg_id not in archive:
            archive[epg_id] = (s["stream_id"], s.get("name", epg_id))
    n_arch = sum(1 for s in streams if str(s.get("tv_archive", "0")) == "1" and s.get("epg_channel_id"))
    print(f"  {len(archive)} channels to query via API ({n_arch} catchup streams)", flush=True)

    # 3) Past listings via API
    def get_listings(epg_id, stream_id):
        data = api("get_simple_data_table", stream_id=stream_id)
        return epg_id, data.get("epg_listings", []) if isinstance(data, dict) else []

    added, failed = 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(get_listings, e, sid) for e, (sid, _) in archive.items()]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                epg_id, listings = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                continue
            for item in listings:
                try:
                    start = int(item["start_timestamp"])
                    stop = int(item["stop_timestamp"])
                except (KeyError, ValueError, TypeError):
                    continue
                if start < cutoff or (epg_id, start) in programmes:
                    continue
                p = ET.Element("programme", start=ts_to_xmltv(start), stop=ts_to_xmltv(stop), channel=epg_id)
                ET.SubElement(p, "title").text = b64(item.get("title"))
                desc = b64(item.get("description"))
                if desc:
                    ET.SubElement(p, "desc").text = desc
                programmes[(epg_id, start)] = p
                added += 1
            if i % 50 == 0:
                print(f"  {i}/{len(futures)} channels processed", flush=True)

    # Catchup channels missing from the XMLTV
    for epg_id, (_, name) in archive.items():
        if epg_id not in channels:
            ch = ET.Element("channel", id=epg_id)
            ET.SubElement(ch, "display-name").text = name
            channels[epg_id] = ch

    # 4) Write output
    out = ET.Element("tv", {"generator-info-name": "uhf-catchup-epg"})
    for ch in channels.values():
        out.append(ch)
    for key in sorted(programmes, key=lambda k: (k[0] or "", k[1])):
        out.append(programmes[key])

    os.makedirs(OUT_DIR, exist_ok=True)
    xml_bytes = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(out, encoding="utf-8")
    with open(os.path.join(OUT_DIR, "epg.xml"), "wb") as f:
        f.write(xml_bytes)
    with gzip.open(os.path.join(OUT_DIR, "epg.xml.gz"), "wb") as f:
        f.write(xml_bytes)

    print(f"Done: {base_count} base programmes + {added} programmes from the API "
          f"({failed} channels failed). Size: {len(xml_bytes) / 1e6:.1f} MB", flush=True)
    if base_count == 0 and added == 0:
        sys.exit("No programmes retrieved - check your credentials/server URL.")


if __name__ == "__main__":
    main()
