#!/usr/bin/env python3
"""Scrape Delta Force collection items from orzice.com.

The site renders the useful data into paginated HTML. This scraper reads:
- /v/collection for tradable collection prices.
- /v/scp_book for the collection handbook, including non-tradable items.

Outputs are written as JSON and CSV. Images are downloaded by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://orzice.com"
CATEGORY_MAP = {
    1: "工艺藏品",
    2: "工具材料",
    3: "电子物品",
    4: "家居物品",
    5: "能源燃料",
    6: "医疗道具",
    7: "资料情报",
}
CSV_FIELDS = [
    "item_id",
    "name",
    "rarity_level",
    "category",
    "image_url",
    "local_image_path",
    "detail_url",
    "trade_status",
    "current_price",
    "price_3d",
    "price_7d",
    "price_30d",
    "today_change_percent",
    "change_3d_percent",
    "change_7d_percent",
    "change_30d_percent",
    "source_pages",
]


@dataclass
class ScraperConfig:
    out_dir: Path
    delay: float
    timeout: float
    download_images: bool
    image_dir_name: str
    max_pages: int | None


class OrziceScraper:
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Referer": BASE_URL + "/v/collection",
            }
        )

    def fetch(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = urljoin(BASE_URL, path)
        response = self.session.get(url, params=params, timeout=self.config.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def sleep(self) -> None:
        if self.config.delay > 0:
            time.sleep(self.config.delay)

    def scrape_collection(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for category_id, category_name in CATEGORY_MAP.items():
            first_html = self.fetch(
                "/v/collection",
                {
                    "a": "collection",
                    "top": "1-2",
                    "p": 1,
                    "grade": "-1",
                    "mtype": category_id,
                    "n": "",
                },
            )
            total = self.extract_collection_count(first_html)
            page_count = max(1, math.ceil(total / 10)) if total else None
            if self.config.max_pages is not None:
                page_count = min(page_count or self.config.max_pages, self.config.max_pages)

            print(f"[collection] {category_name}: total={total or 'unknown'}, pages={page_count or 'until empty'}", flush=True)
            page = 1
            while True:
                html = first_html if page == 1 else self.fetch(
                    "/v/collection",
                    {
                        "a": "collection",
                        "top": "1-2",
                        "p": page,
                        "grade": "-1",
                        "mtype": category_id,
                        "n": "",
                    },
                )
                page_items = self.parse_collection_page(html, category_id, category_name)
                if not page_items:
                    break
                items.extend(page_items)
                if page_count is not None and page >= page_count:
                    break
                page += 1
                self.sleep()
        return items

    @staticmethod
    def extract_collection_count(html: str) -> int | None:
        match = re.search(r"this\.count\s*=\s*(\d+)", html)
        return int(match.group(1)) if match else None

    def parse_collection_page(self, html: str, category_id: int, category_name: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict[str, Any]] = []
        for row in soup.select("table.modern-table tbody tr.table-row"):
            cells = row.select("td")
            if len(cells) < 10:
                continue

            link = row.select_one("a.item-avatar[href]")
            image = row.select_one("img")
            name_node = row.select_one(".item-name")
            if not link or not image or not name_node:
                continue

            item_id = self.extract_item_id(link.get("href", ""))
            detail_url = urljoin(BASE_URL, link.get("href", ""))
            onclick_text = " ".join(cell.get("onclick", "") for cell in cells)
            detail_image_url = self.extract_js_pic(onclick_text)
            image_url = detail_image_url or urljoin(BASE_URL, image.get("src", ""))

            items.append(
                {
                    "item_id": item_id,
                    "name": clean_text(name_node.get_text(" ", strip=True)),
                    "rarity_level": parse_int(image.get("data-grade")),
                    "category_id": category_id,
                    "category": category_name,
                    "image_url": image_url,
                    "local_image_path": "",
                    "detail_url": detail_url,
                    "trade_status": self.extract_trade_status(row),
                    "current_price": extract_price_cell(cells[2]),
                    "price_3d": extract_price_cell(cells[4]),
                    "price_7d": extract_price_cell(cells[6]),
                    "price_30d": extract_price_cell(cells[8]),
                    "today_change_percent": extract_percent_cell(cells[3]),
                    "change_3d_percent": extract_percent_cell(cells[5]),
                    "change_7d_percent": extract_percent_cell(cells[7]),
                    "change_30d_percent": extract_percent_cell(cells[9]),
                    "source_pages": ["collection"],
                }
            )
        return items

    @staticmethod
    def extract_trade_status(row: Tag) -> str:
        grade = row.select_one(".item-grade")
        text = clean_text(grade.get_text(" ", strip=True)) if grade else ""
        return text.replace("推荐方式：", "").strip()

    @staticmethod
    def extract_js_pic(onclick_text: str) -> str:
        match = re.search(r"pic\s*:\s*'([^']+)'", onclick_text)
        return match.group(1) if match else ""

    def scrape_book(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for category_id, category_name in CATEGORY_MAP.items():
            print(f"[scp_book] {category_name}: crawling", flush=True)
            page = 1
            while True:
                if self.config.max_pages is not None and page > self.config.max_pages:
                    break
                html = self.fetch(
                    "/v/scp_book",
                    {
                        "top": "3-2",
                        "grade": "-1",
                        "mtype": category_id,
                        "n": "",
                        "p": page,
                    },
                )
                page_items = self.parse_book_page(html, category_id, category_name)
                if not page_items:
                    break
                items.extend(page_items)
                if not has_next_page(html):
                    break
                page += 1
                self.sleep()
        return items

    def parse_book_page(self, html: str, category_id: int, category_name: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict[str, Any]] = []
        for card in soup.select("a.item-card[href]"):
            image = card.select_one("img")
            name_node = card.select_one(".item-name")
            if not image or not name_node:
                continue

            href = card.get("href", "")
            item_id = self.extract_item_id(href)
            badge = card.select_one(".badge")
            items.append(
                {
                    "item_id": item_id,
                    "name": clean_text(name_node.get("title") or name_node.get_text(" ", strip=True)),
                    "rarity_level": parse_int(image.get("data-grade")),
                    "category_id": category_id,
                    "category": category_name,
                    "image_url": urljoin(BASE_URL, image.get("src", "")),
                    "local_image_path": "",
                    "detail_url": urljoin(BASE_URL, href),
                    "trade_status": clean_text(badge.get_text(" ", strip=True)) if badge else "",
                    "current_price": None,
                    "price_3d": None,
                    "price_7d": None,
                    "price_30d": None,
                    "today_change_percent": None,
                    "change_3d_percent": None,
                    "change_7d_percent": None,
                    "change_30d_percent": None,
                    "source_pages": ["scp_book"],
                }
            )
        return items

    @staticmethod
    def extract_item_id(href: str) -> str:
        return href.rstrip("/").split("/")[-1]

    def download_image(self, item: dict[str, Any], image_dir: Path) -> None:
        image_url = item.get("image_url")
        if not image_url:
            return
        suffix = image_suffix(image_url)
        target = image_dir / f"{safe_filename(item['item_id'] + '_' + item['name'])}{suffix}"
        item["local_image_path"] = str(target.as_posix())
        if target.exists() and target.stat().st_size > 0:
            return

        response = self.session.get(image_url, timeout=self.config.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if target.suffix == ".img":
            detected = suffix_from_content_type(content_type)
            if detected != ".img":
                target = target.with_suffix(detected)
                item["local_image_path"] = str(target.as_posix())
        target.write_bytes(response.content)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "")
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def parse_float(value: str) -> float | None:
    text = value.replace(",", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def extract_price_cell(cell: Tag) -> int | None:
    icon = cell.select_one(".icon-gold")
    text = icon.get_text(" ", strip=True) if icon else cell.get_text(" ", strip=True)
    return parse_int(text)


def extract_percent_cell(cell: Tag) -> float | None:
    badge = cell.select_one(".change-badge")
    text = badge.get_text(" ", strip=True) if badge else cell.get_text(" ", strip=True)
    return parse_float(text)


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return any("下一页" in a.get_text(" ", strip=True) for a in soup.find_all("a"))


def merge_items(collection_items: list[dict[str, Any]], book_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in book_items + collection_items:
        key = item["item_id"]
        if key not in merged:
            merged[key] = item.copy()
            continue
        existing = merged[key]
        for field, value in item.items():
            if field == "source_pages":
                existing["source_pages"] = sorted(set(existing.get("source_pages", [])) | set(value))
            elif value not in (None, "", []):
                existing[field] = value
    return sorted(
        merged.values(),
        key=lambda x: (
            x.get("category_id") or 99,
            -(x.get("rarity_level") or 0),
            x.get("name") or "",
        ),
    )


def write_json(items: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(items: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = item.copy()
            row["source_pages"] = ",".join(row.get("source_pages", []))
            writer.writerow(row)


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    return value[:150] or "image"


def image_suffix(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".img"


def suffix_from_content_type(content_type: str) -> str:
    content_type = content_type.lower()
    if "png" in content_type:
        return ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ".img"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Delta Force collection resources from orzice.com.")
    parser.add_argument("--out-dir", default="data", help="Output directory. Default: data")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between page requests in seconds. Default: 0.2")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP timeout in seconds. Default: 20")
    parser.add_argument("--skip-images", action="store_true", help="Do not download item images.")
    parser.add_argument("--image-dir", default="images", help="Image subdirectory name. Default: images")
    parser.add_argument("--max-pages", type=int, default=None, help="Debug option: limit pages per category.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    config = ScraperConfig(
        out_dir=out_dir,
        delay=args.delay,
        timeout=args.timeout,
        download_images=not args.skip_images,
        image_dir_name=args.image_dir,
        max_pages=args.max_pages,
    )
    scraper = OrziceScraper(config)

    collection_items = scraper.scrape_collection()
    book_items = scraper.scrape_book()
    items = merge_items(collection_items, book_items)

    if config.download_images:
        image_dir = out_dir / config.image_dir_name
        image_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(items, start=1):
            try:
                scraper.download_image(item, image_dir)
            except Exception as exc:  # Keep metadata even if one image fails.
                print(f"[image] failed: {item.get('name')} {item.get('image_url')} ({exc})", flush=True)
            if index % 25 == 0:
                print(f"[image] downloaded/checked {index}/{len(items)}", flush=True)
            scraper.sleep()

    write_json(items, out_dir / "deltaforce_collections.json")
    write_csv(items, out_dir / "deltaforce_collections.csv")
    print(f"Done. items={len(items)}", flush=True)
    print(f"JSON: {out_dir / 'deltaforce_collections.json'}", flush=True)
    print(f"CSV : {out_dir / 'deltaforce_collections.csv'}", flush=True)


if __name__ == "__main__":
    main()
