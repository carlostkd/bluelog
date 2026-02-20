#!/usr/bin/env python3
import asyncio
import hashlib
import json
import os
import signal
import time
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bleak import BleakScanner

SAVE_PATH = "/home/bluelog.json"
FLUSH_INTERVAL = 10
MAX_LAST_EVENTS = 25


#comment this lines if you dont use HA
NEW_DEVICE_WEBHOOK_URL = "http://127.0.0.1:8123/api/webhook/ble_new_device"
WEBHOOK_TIMEOUT = 3

SCANNING_MODE = "active"
BLE_ADAPTER = "hci1"  

# Presence: mark left after N seconds unseen; notify only on ENTER
LEAVE_TIMEOUT = 120

# Suppress notifications for specific device names (case-insensitive exact match)
SUPPRESS_NOTIFY_NAMES = {"your_devices_here"}



def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> float:
    return time.time()


def compute_fingerprint(
    mfg_ids: List[int],
    service_uuids: List[str],
    service_data_keys: List[str],
) -> Tuple[str, Dict[str, Any]]:
    mfg_part = ",".join(str(x) for x in sorted(set(mfg_ids or [])))
    svc_part = ",".join(sorted(set(service_uuids or [])))
    sdk_part = ",".join(sorted(set(service_data_keys or [])))
    raw = f"mfg={mfg_part}|svc={svc_part}|sdk={sdk_part}"
    fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    material = {
        "manufacturer_ids": sorted(set(mfg_ids or [])),
        "service_uuids": sorted(set(service_uuids or [])),
        "service_data_keys": sorted(set(service_data_keys or [])),
    }
    return fp, material


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_registry(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"meta": {"created_utc": utc_now_iso()}, "devices": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return {"meta": {"created_utc": utc_now_iso()}, "devices": {}}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"meta": {"created_utc": utc_now_iso()}, "devices": {}}
            data.setdefault("meta", {})
            data.setdefault("devices", {})
            return data
    except Exception:
        return {"meta": {"created_utc": utc_now_iso()}, "devices": {}}


def classify_tags(mfg_ids: List[int], service_uuids: List[str]) -> Tuple[List[str], str]:
    tags: List[str] = []
    if 76 in (mfg_ids or []):
        tags.append("vendor:apple")
    if 117 in (mfg_ids or []):
        tags.append("vendor:samsung")
    if 224 in (mfg_ids or []):
        tags.append("vendor:google")
    if any((u or "").lower().startswith("0000feaa") for u in (service_uuids or [])):
        tags.append("beacon:eddystone")
    primary = tags[0] if tags else "unclassified"
    return sorted(set(tags)), primary


def post_webhook_async(url: str, payload: Dict[str, Any]) -> None:
    def _send():
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
                r.read()
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()




class BLEVisitorLogger:
    def __init__(self, path: str):
        self.path = path
        self.registry = load_registry(path)
        self.registry.setdefault("meta", {})
        self.registry.setdefault("devices", {})
        self.dirty = False
        self.last_flush = time.time()
        self.stop = False

        self.registry["meta"]["updated_utc"] = utc_now_iso()
        self.registry["meta"]["adapter"] = BLE_ADAPTER
        self.registry["meta"]["leave_timeout_sec"] = LEAVE_TIMEOUT

        self._migrate_presence_fields()

    def _migrate_presence_fields(self) -> None:
        for rec in self.registry.get("devices", {}).values():
            rec.setdefault("present", False)
            rec.setdefault("last_seen_ts", 0.0)
            rec.setdefault("last_left", None)
            rec.setdefault("last_enter", None)

            rec.setdefault("enter_count", 0)
            rec.setdefault("leave_count", 0)
            rec.setdefault("reenter_count", 0)

            rec.setdefault("last_enter_ts", None)
            rec.setdefault("last_left_ts", None)

            rec.setdefault("presence_by_day", {})

        self.dirty = True

    def _name_suppressed(self, name: Optional[str]) -> bool:
        n = (name or "").strip().lower()
        return n in SUPPRESS_NOTIFY_NAMES

    def _qualifies_for_notify(self, rec: Dict[str, Any], name: Optional[str]) -> bool:
        if self._name_suppressed(name):
            return False
        has_name = bool((name or "").strip())
        has_vendor = rec.get("primary_tag") != "unclassified"
        return has_name or has_vendor

    def _update_presence_time(self, rec: Dict[str, Any], leave_ts: float) -> None:
        """
        Called when a device transitions from present → absent.
        Calculates the elapsed time since the last ENTER and adds it
        to the per‑day aggregation stored in `presence_by_day`.
        """
        enter_ts = rec.get("last_enter_ts")
        if not enter_ts:
            return

        elapsed = max(0.0, leave_ts - enter_ts)
        day_key = datetime.fromtimestamp(enter_ts, tz=timezone.utc).date().isoformat()
        agg = rec.setdefault("presence_by_day", {})
        agg[day_key] = agg.get(day_key, 0.0) + elapsed

        rec["last_left_ts"] = leave_ts

    def mark_left_if_stale(self) -> None:
        now_ts = now_epoch()
        now_iso = utc_now_iso()
        for rec in self.registry["devices"].values():
            if not rec.get("present", False):
                continue
            last_seen_ts = float(rec.get("last_seen_ts") or 0.0)
            if last_seen_ts <= 0:
                continue
            if (now_ts - last_seen_ts) >= LEAVE_TIMEOUT:
                rec["present"] = False
                rec["last_left"] = now_iso
                rec["leave_count"] = int(rec.get("leave_count") or 0) + 1

                self._update_presence_time(rec, now_ts)

                self.dirty = True
        if self.dirty:
            self.registry["meta"]["updated_utc"] = now_iso

    def upsert(
        self,
        address: Optional[str],
        name: Optional[str],
        rssi: Optional[int],
        mfg_data: Dict[int, bytes],
        service_uuids: List[str],
        service_data_keys: List[str],
        source: Optional[str],
    ) -> None:
        now_iso = utc_now_iso()
        now_ts = now_epoch()

        mfg_ids = list((mfg_data or {}).keys())
        fp, material = compute_fingerprint(mfg_ids, service_uuids, service_data_keys)

        devices = self.registry["devices"]
        rec = devices.get(fp)

        if rec is None:
            tags, primary = classify_tags(mfg_ids, service_uuids)
            rec = {
                "fp": fp,
                "fingerprint": material,
                "tags": tags,
                "primary_tag": primary,
                "first_seen": now_iso,
                "last_seen": now_iso,
                "seen_count": 0,
                "addresses": [],
                "names_seen": [],
                "best_rssi": None,
                "last_rssi": None,
                "last_events": [],
                "present": False,
                "last_seen_ts": 0.0,
                "last_left": None,
                "last_enter": None,
                "last_enter_ts": None,
                "last_left_ts": None,
                "enter_count": 0,
                "leave_count": 0,
                "reenter_count": 0,
                "presence_by_day": {},
            }
            devices[fp] = rec

        rec["last_seen"] = now_iso
        rec["last_seen_ts"] = now_ts
        rec["seen_count"] = int(rec.get("seen_count") or 0) + 1
        rec["last_rssi"] = rssi

        if rssi is not None:
            best = rec.get("best_rssi")
            if best is None or rssi > best:
                rec["best_rssi"] = rssi

        if address and address not in rec["addresses"]:
            rec["addresses"].append(address)

        if name and name not in rec["names_seen"]:
            rec["names_seen"].append(name)

        rec["last_events"].append(
            {"time": now_iso, "address": address, "rssi": rssi, "source": source}
        )
        rec["last_events"] = rec["last_events"][-MAX_LAST_EVENTS:]

        self.registry["meta"]["updated_utc"] = now_iso
        self.dirty = True

        if not rec.get("present", False):
            rec["enter_count"] = int(rec.get("enter_count") or 0) + 1
            if rec.get("last_left") is not None:
                rec["reenter_count"] = int(rec.get("reenter_count") or 0) + 1

            rec["present"] = True
            rec["last_enter"] = now_iso
            rec["last_enter_ts"] = now_ts

            if self._qualifies_for_notify(rec, name):
                payload = {
                    "event": "enter",
                    "fp": fp,
                    "name": name,
                    "address": address,
                    "rssi": rssi,
                    "best_rssi": rec.get("best_rssi"),
                    "seen_count": rec.get("seen_count"),
                    "primary_tag": rec.get("primary_tag"),
                    "tags": rec.get("tags", []),
                    "last_left": rec.get("last_left"),
                    "last_enter": rec.get("last_enter"),
                    "manufacturer_ids": material.get("manufacturer_ids", []),
                    "service_uuids": material.get("service_uuids", []),
                    "service_data_keys": material.get("service_data_keys", []),
                }
                post_webhook_async(NEW_DEVICE_WEBHOOK_URL, payload)

    async def run(self) -> None:
        def cb(device, adv):
            try:
                mfg_data = dict(getattr(adv, "manufacturer_data", None) or {})
                svc_uuids = list(getattr(adv, "service_uuids", None) or [])
                svc_data_keys = list((getattr(adv, "service_data", None) or {}).keys())
                adv_name = getattr(adv, "local_name", None)
                name = adv_name or getattr(device, "name", None)

                self.upsert(
                    address=getattr(device, "address", None),
                    name=name,
                    rssi=getattr(adv, "rssi", None),
                    mfg_data=mfg_data,
                    service_uuids=svc_uuids,
                    service_data_keys=svc_data_keys,
                    source=getattr(adv, "source", None),
                )
            except Exception:
                pass

        scanner = BleakScanner(
            detection_callback=cb,
            scanning_mode=SCANNING_MODE,
            adapter=BLE_ADAPTER,
        )

        async with scanner:
            while not self.stop:
                await asyncio.sleep(1)
                self.mark_left_if_stale()
                if self.dirty and (time.time() - self.last_flush) > FLUSH_INTERVAL:
                    atomic_write_json(self.path, self.registry)
                    self.last_flush = time.time()
                    self.dirty = False

        if self.dirty:
            atomic_write_json(self.path, self.registry)

    def stop_req(self, *_):
        self.stop = True


async def main():
    logger = BLEVisitorLogger(SAVE_PATH)
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, logger.stop_req)
        except Exception:
            pass
    await logger.run()


if __name__ == "__main__":
    asyncio.run(main())
