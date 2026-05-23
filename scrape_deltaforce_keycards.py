#!/usr/bin/env python3
"""Scrape Delta Force keycard room data from kkrb.net.

The page is rendered from an AJAX endpoint. This scraper keeps the browser-like
initialization sequence so it can be rerun without manual cookies:
- open the target page and apply the JavaScript cookie challenge;
- call getMenu to obtain the current app version;
- call checkUAStatus, then getKeycardRoomContainerData.

Outputs are written as JSON and CSV. Keycard images and container icons are
downloaded by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://www.kkrb.net/"
TARGET_ROUTE = "view/map/keycard_room"
TARGET_URL = BASE_URL + "?viewpage=view%2Fmap%2Fkeycard_room"
TARGET_PAGE = "pages/map/keycard_room.html?t=1748765200"

PLACE_NAMES = {
    "db": "零号大坝",
    "cgxg": "长弓溪谷",
    "bks": "巴克什",
    "htjd": "航天基地",
    "cxjy": "潮汐监狱",
}

CONTAINER_NAMES = {
    "openDoor": "仅开门",
    "openDoorMuilt": "一卡多开",
    "openBridge": "仅放桥",
    "strongBox": "保险箱",
    "storageBox": "收纳盒",
    "medicalSuppliesPile": "医疗物资堆",
    "smallStrongBox": "小保险箱",
    "clothing": "一件衣服",
    "advancedSuitcase": "高级旅行箱",
    "mountaineeringBag": "登山包",
    "advancedStorageBin": "高级储物箱",
    "displayCabinet": "展示柜",
    "storageCabinet": "储物柜",
    "drawerCabinet": "抽屉柜",
    "toolCabinet": "工具柜",
    "militaryMedicalKit": "军用医疗包",
    "computerCase": "电脑机箱",
    "server": "服务器",
    "aviationStorageBox": "航空储物箱",
    "suitcase": "手提箱",
    "decipherLaptop": "待破译笔记本电脑",
    "largeWeaponBox": "大武器箱",
    "weaponBox": "武器箱",
    "ammoBox": "弹药箱",
    "expressBox": "快递箱",
    "scatteredPoint": "散落物品随机刷新点",
}

CSV_FIELDS = [
    "item_id",
    "name",
    "level",
    "keycard_type",
    "price",
    "durability",
    "use_map",
    "use_place",
    "place_code",
    "place_name",
    "room_name",
    "containers",
    "container_json",
    "consume_normal",
    "consume_secret",
    "consume_top_secret",
    "single_cost_normal",
    "single_cost_secret",
    "single_cost_top_secret",
    "image_url",
    "local_image_path",
    "room_place_pics",
    "container_image_paths",
    "price_curve_points",
    "data_version",
]


class KkrbKeycardScraper:
    def __init__(self, delay: float, timeout: float) -> None:
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Referer": TARGET_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)

    def initialize_session(self) -> str:
        response = self.session.get(TARGET_URL, timeout=self.timeout)
        response.raise_for_status()

        match = re.search(r"document\.cookie\s*=\s*'([^=]+)=([^']+)'", response.text)
        if match:
            self.session.cookies.set(match.group(1), match.group(2), domain="www.kkrb.net", path="/")
            response = self.session.get(TARGET_URL, timeout=self.timeout)
            response.raise_for_status()

        # Loading the child page is not strictly required for the API, but it
        # keeps this script aligned with the website's actual render flow.
        page = self.session.get(urljoin(BASE_URL, TARGET_PAGE), timeout=self.timeout)
        page.raise_for_status()
        return page.text

    def fetch_menu(self) -> dict[str, Any]:
        data = self.post_json("getMenu", {"globalData": "false"})
        if data.get("code") != 1:
            raise RuntimeError(f"getMenu failed: {data}")
        return data

    def check_user_agent(self, version: str) -> None:
        data = self.post_json("checkUAStatus", {"version": version})
        if data.get("code") != 1:
            raise RuntimeError(f"checkUAStatus failed: {data}")

    def fetch_keycards(self, version: str) -> dict[str, Any]:
        data = self.post_json("getKeycardRoomContainerData", {"version": version})
        if data.get("code") != 1:
            raise RuntimeError(f"getKeycardRoomContainerData failed: {data}")
        return data

    def post_json(self, endpoint: str, data: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        last_data: dict[str, Any] = {}
        for attempt in range(1, retries + 1):
            response = self.session.post(
                urljoin(BASE_URL, endpoint),
                data=data,
                timeout=self.timeout,
            )
            response.raise_for_status()
            last_data = response.json()
            if last_data.get("code") != -101:
                return last_data

            print(f"[retry] {endpoint} returned -101, refreshing session ({attempt}/{retries})", flush=True)
            self.sleep()
            self.initialize_session()
        return last_data

    def scrape(self) -> tuple[list[dict[str, Any]], str]:
        self.initialize_session()
        menu = self.fetch_menu()
        version = str(menu.get("built_ver") or "")
        self.check_user_agent(version)
        payload = self.fetch_keycards(version)
        data_version = str(payload.get("version") or "")
        items = [normalize_item(item, data_version) for item in payload.get("data", [])]
        return items, data_version

    def download_url(self, url: str, target: Path) -> Path | None:
        if not url:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0:
            return target

        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        target.write_bytes(response.content)
        self.sleep()
        return target


def normalize_item(item: dict[str, Any], data_version: str) -> dict[str, Any]:
    detail = item.get("itemDetail") or {}
    place_code = str(item.get("place") or "")
    normalized = {
        "item_id": str(item.get("itemID") or ""),
        "name": str(item.get("itemName") or ""),
        "level": item.get("itemLevel"),
        "keycard_type": detail.get("keyCardType"),
        "price": item.get("itemPrice"),
        "durability": detail.get("durability"),
        "use_map": detail.get("useMap"),
        "use_place": detail.get("usePlace"),
        "place_code": place_code,
        "place_name": PLACE_NAMES.get(place_code, place_code),
        "consume": item.get("placeConsume") or {},
        "image_url": item.get("itemPic") or "",
        "local_image_path": "",
        "rooms": [],
        "price_curve": item.get("priceCurve") or {},
        "data_version": data_version,
        "raw": item,
    }

    for room in item.get("itemRooms") or []:
        containers = room.get("containers") or {}
        normalized["rooms"].append(
            {
                "room_name": room.get("roomName") or "",
                "room_place_pics": room.get("roomPlacePic") or [],
                "containers": containers,
                "container_details": [
                    {
                        "container_id": container_id,
                        "container_name": CONTAINER_NAMES.get(container_id, container_id),
                        "quantity": quantity,
                        "image_url": urljoin(BASE_URL, f"image/map/keycard_room/container/{container_id}.webp"),
                        "local_image_path": "",
                    }
                    for container_id, quantity in containers.items()
                ],
            }
        )
    return normalized


def enrich_images(scraper: KkrbKeycardScraper, items: list[dict[str, Any]], out_dir: Path, image_dir_name: str) -> None:
    keycard_dir = out_dir / image_dir_name / "keycards"
    container_dir = out_dir / image_dir_name / "containers"
    container_path_cache: dict[str, str] = {}

    for index, item in enumerate(items, start=1):
        image_url = item.get("image_url") or ""
        suffix = image_suffix(image_url)
        keycard_target = keycard_dir / f"{safe_filename(item['item_id'] + '_' + item['name'])}{suffix}"
        try:
            path = scraper.download_url(image_url, keycard_target)
            if path:
                item["local_image_path"] = path.as_posix()
        except Exception as exc:
            print(f"[image] keycard failed: {item.get('name')} {image_url} ({exc})", flush=True)

        for room in item.get("rooms", []):
            for container in room.get("container_details", []):
                container_id = container["container_id"]
                if container_id not in container_path_cache:
                    target = container_dir / f"{safe_filename(container_id + '_' + container['container_name'])}.webp"
                    try:
                        path = scraper.download_url(container["image_url"], target)
                        if path:
                            container_path_cache[container_id] = path.as_posix()
                    except Exception as exc:
                        print(f"[image] container failed: {container_id} ({exc})", flush=True)
                        container_path_cache[container_id] = ""
                container["local_image_path"] = container_path_cache.get(container_id, "")

        if index % 20 == 0:
            print(f"[image] downloaded/checked {index}/{len(items)}", flush=True)


def flatten_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        consume = item.get("consume") or {}
        for room in item.get("rooms", []):
            containers = room.get("containers") or {}
            container_details = room.get("container_details") or []
            rows.append(
                {
                    "item_id": item.get("item_id"),
                    "name": item.get("name"),
                    "level": item.get("level"),
                    "keycard_type": item.get("keycard_type"),
                    "price": item.get("price"),
                    "durability": item.get("durability"),
                    "use_map": item.get("use_map"),
                    "use_place": item.get("use_place"),
                    "place_code": item.get("place_code"),
                    "place_name": item.get("place_name"),
                    "room_name": room.get("room_name"),
                    "containers": format_containers(containers),
                    "container_json": json.dumps(containers, ensure_ascii=False, separators=(",", ":")),
                    "consume_normal": consume.get("normal"),
                    "consume_secret": consume.get("secret"),
                    "consume_top_secret": consume.get("top_secret"),
                    "single_cost_normal": calc_single_cost(item, consume.get("normal")),
                    "single_cost_secret": calc_single_cost(item, consume.get("secret")),
                    "single_cost_top_secret": calc_single_cost(item, consume.get("top_secret")),
                    "image_url": item.get("image_url"),
                    "local_image_path": item.get("local_image_path"),
                    "room_place_pics": "|".join(room.get("room_place_pics") or []),
                    "container_image_paths": "|".join(
                        path
                        for path in (container.get("local_image_path") for container in container_details)
                        if path
                    ),
                    "price_curve_points": len(item.get("price_curve") or {}),
                    "data_version": item.get("data_version"),
                }
            )
    return rows


def calc_single_cost(item: dict[str, Any], consume: Any) -> int | None:
    price = item.get("price")
    durability = item.get("durability")
    if not consume or not price or not durability:
        return None
    return round((float(price) / float(durability)) * float(consume))


def format_containers(containers: dict[str, Any]) -> str:
    return "; ".join(
        f"{CONTAINER_NAMES.get(container_id, container_id)} x{quantity}"
        for container_id, quantity in containers.items()
    )


def write_json(items: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    return value[:150] or "image"


def image_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Delta Force keycard room data from kkrb.net.")
    parser.add_argument("--out-dir", default="data/keycards", help="Output directory. Default: data/keycards")
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between image requests in seconds. Default: 0.1")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP timeout in seconds. Default: 20")
    parser.add_argument("--skip-images", action="store_true", help="Do not download keycard/container images.")
    parser.add_argument("--image-dir", default="images", help="Image subdirectory name. Default: images")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scraper = KkrbKeycardScraper(delay=args.delay, timeout=args.timeout)
    items, data_version = scraper.scrape()
    print(f"[api] data_version={data_version}, keycards={len(items)}", flush=True)

    if not args.skip_images:
        enrich_images(scraper, items, out_dir, args.image_dir)

    rows = flatten_rows(items)
    write_json(items, out_dir / "deltaforce_keycards.json")
    write_csv(rows, out_dir / "deltaforce_keycards.csv")

    print(f"Done. keycards={len(items)}, rows={len(rows)}", flush=True)
    print(f"JSON: {out_dir / 'deltaforce_keycards.json'}", flush=True)
    print(f"CSV : {out_dir / 'deltaforce_keycards.csv'}", flush=True)


if __name__ == "__main__":
    main()
