#!/usr/bin/env python3
"""Scrape Delta Force container data from kkrb.net.

The site is a dynamic Layui app. This scraper calls the same JSON endpoints used
by the two container pages:
- /getCIEData for the container catalogue and item-size rules.
- /getLCSData for the loot-container simulator list and simulated loot results.

Outputs are written as JSON and CSV. Container and loot images are downloaded by
default. Drop rates are estimates calculated from simulator samples, not static
official probabilities.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://www.kkrb.net/"
ENTRY_URL = urljoin(BASE_URL, "?viewpage=view%2Fminigame%2Floot_container_simulator")
CONTAINER_PAGE_URL = urljoin(BASE_URL, "?viewpage=view%2Ftutorial%2Fcontainer_item")

CONTAINER_CSV_FIELDS = [
    "container_id",
    "name",
    "category",
    "image_url",
    "local_image_path",
    "item_types",
    "guaranteed_item_sizes",
    "possible_item_sizes",
    "excluded_item_sizes",
    "supports_simulator",
    "simulator_avg_profit",
    "sample_draws",
    "sample_items_total",
    "grade_drop_rates",
]

DROP_CSV_FIELDS = [
    "container_id",
    "container_name",
    "item_name",
    "grade",
    "image_url",
    "local_image_path",
    "width",
    "height",
    "current_price",
    "sample_draws",
    "occurrences",
    "draw_hits",
    "occurrences_per_draw",
    "draw_hit_rate",
]


@dataclass
class ScraperConfig:
    out_dir: Path
    delay: float
    timeout: float
    download_images: bool
    container_image_dir_name: str
    loot_image_dir_name: str
    samples_per_container: int
    draw_batch_size: int
    page_size: int
    container_ids: set[str] | None
    try_all_simulation: bool


class KkrbContainerScraper:
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
                "Referer": ENTRY_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.app_version = ""

    def bootstrap(self) -> None:
        """Acquire the anti-refresh cookie and app version used by APIs."""
        response = self.session.get(ENTRY_URL, timeout=self.config.timeout)
        token = extract_yxd_token(response.text)
        if token:
            self.session.cookies.set("yxd_token", token)
            self.session.get(ENTRY_URL, timeout=self.config.timeout).raise_for_status()

        menu = self.post_json("getMenu", {"globalData": "false"})
        self.app_version = str(menu.get("built_ver") or "")
        if not self.app_version:
            raise RuntimeError("Unable to read app version from getMenu")

    def post_json(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(BASE_URL, endpoint)
        for attempt in range(1, 5):
            response = self.session.post(url, data=data, timeout=self.config.timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"{endpoint} returned non-object JSON")
            code = payload.get("code")
            if code is None or code == 1 or attempt == 4:
                return payload
            wait_seconds = max(self.config.delay, 0.5) * attempt
            print(f"[api] {endpoint} code={code}, retry {attempt}/4 after {wait_seconds:.1f}s", flush=True)
            time.sleep(wait_seconds)
        raise RuntimeError(f"{endpoint} request failed")

    def sleep(self) -> None:
        if self.config.delay > 0:
            time.sleep(self.config.delay)

    def fetch_container_catalogue(self) -> list[dict[str, Any]]:
        payload = self.post_json("getCIEData", {"version": self.app_version})
        if payload.get("code") != 1:
            raise RuntimeError(f"getCIEData failed: {payload.get('msg') or payload}")
        return list(payload.get("data") or [])

    def fetch_simulator_containers(self) -> list[dict[str, Any]]:
        payload = self.post_json(
            "getLCSData",
            {
                "version": self.app_version,
                "type": "container",
                "pageSize": self.config.page_size,
            },
        )
        if payload.get("code") != 1:
            raise RuntimeError(f"getLCSData container failed: {payload.get('msg') or payload}")
        data = payload.get("data") or {}
        return list(data.get("containers") or [])

    def simulate_loot(self, container_id: str, draw_count: int) -> tuple[list[dict[str, Any]], float | None]:
        payload = self.post_json(
            "getLCSData",
            {
                "version": self.app_version,
                "type": "lootData",
                "cId": container_id,
                "drawCount": draw_count,
            },
        )
        if payload.get("code") != 1:
            raise RuntimeError(str(payload.get("msg") or payload))
        data = payload.get("data") or {}
        results = data.get("results") or []
        avg_profit = parse_float(data.get("avgProfit"))
        return list(results), avg_profit

    def run_simulations(self, containers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if self.config.samples_per_container <= 0:
            return {}

        stats: dict[str, dict[str, Any]] = {}
        for container in containers:
            container_id = str(container["container_id"])
            target = self.config.samples_per_container
            remaining = target
            all_draws: list[dict[str, Any]] = []
            avg_profit_values: list[float] = []

            print(f"[simulate] {container_id} {container['name']}: samples={target}", flush=True)
            while remaining > 0:
                batch = min(remaining, self.config.draw_batch_size)
                try:
                    draws, avg_profit = self.simulate_loot(container_id, batch)
                except Exception as exc:
                    print(f"[simulate] skipped {container_id}: {exc}", flush=True)
                    break
                all_draws.extend(draws[:batch])
                if avg_profit is not None:
                    avg_profit_values.append(avg_profit)
                remaining -= batch
                self.sleep()

            if all_draws:
                stats[container_id] = aggregate_draws(
                    container_id=container_id,
                    container_name=str(container["name"]),
                    draws=all_draws,
                    avg_profit_values=avg_profit_values,
                )
        return stats

    def download_image(self, url: str, target_dir: Path, filename_stem: str) -> str:
        image_url = normalize_url(url)
        if not image_url:
            return ""

        suffix = image_suffix(image_url)
        target = target_dir / f"{safe_filename(filename_stem)}{suffix}"
        if target.exists() and target.stat().st_size > 0:
            return str(target.as_posix())

        response = self.session.get(image_url, timeout=self.config.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if target.suffix == ".img":
            detected = suffix_from_content_type(content_type)
            if detected != ".img":
                target = target.with_suffix(detected)
        target.write_bytes(response.content)
        return str(target.as_posix())


def extract_yxd_token(html: str) -> str:
    match = re.search(r"yxd_token=([0-9a-f]+)", html)
    return match.group(1) if match else ""


def normalize_url(value: Any) -> str:
    if not value:
        return ""
    return urljoin(BASE_URL, str(value).replace("\\/", "/"))


def split_br_text(value: Any) -> list[str]:
    text = str(value or "").replace("\r", "")
    parts = re.split(r"<br\s*/?>|\n|;", text)
    return [clean_text(part) for part in parts if clean_text(part)]


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group(0)) if match else None


def merge_container_sources(
    catalogue_items: list[dict[str, Any]],
    simulator_items: list[dict[str, Any]],
    selected_ids: set[str] | None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for item in catalogue_items:
        container_id = str(item.get("container_id") or "")
        if not container_id:
            continue
        merged[container_id] = {
            "container_id": container_id,
            "name": clean_text(str(item.get("container_name") or "")),
            "category": clean_text(str(item.get("category") or "")),
            "image_url": normalize_url(item.get("container_pic")),
            "local_image_path": "",
            "item_types": normalize_item_types(item.get("item_type")),
            "guaranteed_item_sizes": split_br_text(item.get("real_item_size")),
            "possible_item_sizes": split_br_text(item.get("maybe_item_size")),
            "excluded_item_sizes": split_br_text(item.get("none_item_size")),
            "supports_simulator": False,
            "simulator_avg_profit": None,
            "sample_draws": 0,
            "sample_items_total": 0,
            "grade_drop_rates": {},
            "possible_outputs": [],
            "source_pages": ["container_item"],
        }

    for item in simulator_items:
        container_id = str(item.get("containerId") or "")
        if not container_id:
            continue
        existing = merged.setdefault(
            container_id,
            {
                "container_id": container_id,
                "name": "",
                "category": "",
                "image_url": "",
                "local_image_path": "",
                "item_types": [],
                "guaranteed_item_sizes": [],
                "possible_item_sizes": [],
                "excluded_item_sizes": [],
                "supports_simulator": False,
                "simulator_avg_profit": None,
                "sample_draws": 0,
                "sample_items_total": 0,
                "grade_drop_rates": {},
                "possible_outputs": [],
                "source_pages": [],
            },
        )
        existing["name"] = existing["name"] or clean_text(str(item.get("containerName") or ""))
        existing["image_url"] = existing["image_url"] or normalize_url(item.get("containerPic"))
        existing["item_types"] = existing["item_types"] or normalize_item_types(item.get("containerItemType"))
        existing["guaranteed_item_sizes"] = existing["guaranteed_item_sizes"] or split_br_text(
            item.get("containerRealItemSize")
        )
        existing["possible_item_sizes"] = existing["possible_item_sizes"] or split_br_text(
            item.get("containerMaybeItemSize")
        )
        existing["excluded_item_sizes"] = existing["excluded_item_sizes"] or split_br_text(
            item.get("containerNoneItemSize")
        )
        existing["supports_simulator"] = True
        existing["source_pages"] = sorted(set(existing["source_pages"]) | {"loot_container_simulator"})

    containers = list(merged.values())
    if selected_ids is not None:
        containers = [item for item in containers if item["container_id"] in selected_ids]
    return sorted(containers, key=lambda x: (not x["supports_simulator"], x.get("category") or "", x["name"]))


def normalize_item_types(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        result.append({"name": clean_text(str(item.get("name") or "")), "type": str(item.get("type") or "")})
    return result


def aggregate_draws(
    container_id: str,
    container_name: str,
    draws: list[dict[str, Any]],
    avg_profit_values: list[float],
) -> dict[str, Any]:
    sample_draws = len(draws)
    occurrences: Counter[tuple[str, int, str]] = Counter()
    draw_hits: Counter[tuple[str, int, str]] = Counter()
    grade_occurrences: Counter[int] = Counter()
    grade_draw_hits: Counter[int] = Counter()
    item_meta: dict[tuple[str, int, str], dict[str, Any]] = {}
    total_items = 0

    for draw in draws:
        seen_items: set[tuple[str, int, str]] = set()
        seen_grades: set[int] = set()
        for item in draw.get("items") or []:
            name = clean_text(str(item.get("name") or ""))
            grade = int(parse_float(item.get("grade")) or 0)
            img = normalize_url(item.get("img"))
            key = (name, grade, img)
            occurrences[key] += 1
            seen_items.add(key)
            grade_occurrences[grade] += 1
            seen_grades.add(grade)
            total_items += 1
            item_meta[key] = {
                "container_id": container_id,
                "container_name": container_name,
                "item_name": name,
                "grade": grade,
                "image_url": img,
                "local_image_path": "",
                "width": int(parse_float(item.get("w")) or 0),
                "height": int(parse_float(item.get("h")) or 0),
                "current_price": int(parse_float(item.get("currectPrice")) or 0),
                "sample_draws": sample_draws,
            }
        draw_hits.update(seen_items)
        grade_draw_hits.update(seen_grades)

    possible_outputs = []
    for key, occurrence_count in occurrences.most_common():
        row = item_meta[key].copy()
        row["occurrences"] = occurrence_count
        row["draw_hits"] = draw_hits[key]
        row["occurrences_per_draw"] = round(occurrence_count / sample_draws, 6) if sample_draws else 0
        row["draw_hit_rate"] = round(draw_hits[key] / sample_draws, 6) if sample_draws else 0
        possible_outputs.append(row)

    grade_rates = {}
    for grade in sorted(grade_occurrences):
        grade_rates[str(grade)] = {
            "occurrences": grade_occurrences[grade],
            "draw_hits": grade_draw_hits[grade],
            "occurrences_per_draw": round(grade_occurrences[grade] / sample_draws, 6) if sample_draws else 0,
            "draw_hit_rate": round(grade_draw_hits[grade] / sample_draws, 6) if sample_draws else 0,
        }

    avg_profit = None
    if avg_profit_values:
        avg_profit = sum(avg_profit_values) / len(avg_profit_values)

    return {
        "sample_draws": sample_draws,
        "sample_items_total": total_items,
        "simulator_avg_profit": avg_profit,
        "grade_drop_rates": grade_rates,
        "possible_outputs": possible_outputs,
    }


def write_json(payload: Any, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_containers_csv(containers: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CONTAINER_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in containers:
            row = item.copy()
            row["item_types"] = json.dumps(row.get("item_types", []), ensure_ascii=False)
            row["guaranteed_item_sizes"] = "、".join(row.get("guaranteed_item_sizes", []))
            row["possible_item_sizes"] = "、".join(row.get("possible_item_sizes", []))
            row["excluded_item_sizes"] = "、".join(row.get("excluded_item_sizes", []))
            row["grade_drop_rates"] = json.dumps(row.get("grade_drop_rates", {}), ensure_ascii=False)
            writer.writerow(row)


def write_drops_csv(drops: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=DROP_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in drops:
            writer.writerow(item)


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
    parser = argparse.ArgumentParser(description="Scrape Delta Force container resources from kkrb.net.")
    parser.add_argument("--out-dir", default="data/containers", help="Output directory. Default: data/containers")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between API/image requests. Default: 0.2")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds. Default: 30")
    parser.add_argument("--skip-images", action="store_true", help="Do not download container or loot images.")
    parser.add_argument("--container-image-dir", default="images/containers", help="Container image subdirectory.")
    parser.add_argument("--loot-image-dir", default="images/loot", help="Loot image subdirectory.")
    parser.add_argument(
        "--samples-per-container",
        type=int,
        default=100,
        help="Simulator samples per supported container. Use 0 to skip simulation. Default: 100",
    )
    parser.add_argument("--draw-batch-size", type=int, default=100, help="Simulator drawCount per request. Default: 100")
    parser.add_argument("--page-size", type=int, default=999, help="Container list API page size. Default: 999")
    parser.add_argument(
        "--container-id",
        action="append",
        dest="container_ids",
        help="Only scrape selected container id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--try-all-simulation",
        action="store_true",
        help="Try simulator requests for all catalogue containers, not only the simulator list.",
    )
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
        container_image_dir_name=args.container_image_dir,
        loot_image_dir_name=args.loot_image_dir,
        samples_per_container=max(0, args.samples_per_container),
        draw_batch_size=max(1, args.draw_batch_size),
        page_size=max(1, args.page_size),
        container_ids=set(args.container_ids) if args.container_ids else None,
        try_all_simulation=args.try_all_simulation,
    )

    scraper = KkrbContainerScraper(config)
    scraper.bootstrap()
    print(f"[bootstrap] app_version={scraper.app_version}", flush=True)

    catalogue_items = scraper.fetch_container_catalogue()
    simulator_items = scraper.fetch_simulator_containers()
    containers = merge_container_sources(catalogue_items, simulator_items, config.container_ids)
    print(
        f"[containers] catalogue={len(catalogue_items)} simulator={len(simulator_items)} merged={len(containers)}",
        flush=True,
    )

    simulation_targets = [
        item for item in containers if item["supports_simulator"] or config.try_all_simulation
    ]
    simulation_stats = scraper.run_simulations(simulation_targets)
    for item in containers:
        stats = simulation_stats.get(item["container_id"])
        if not stats:
            continue
        item.update(stats)

    all_drops = [
        drop
        for container in containers
        for drop in container.get("possible_outputs", [])
    ]

    if config.download_images:
        container_image_dir = out_dir / config.container_image_dir_name
        loot_image_dir = out_dir / config.loot_image_dir_name
        container_image_dir.mkdir(parents=True, exist_ok=True)
        loot_image_dir.mkdir(parents=True, exist_ok=True)

        for index, container in enumerate(containers, start=1):
            try:
                container["local_image_path"] = scraper.download_image(
                    container["image_url"],
                    container_image_dir,
                    f"{container['container_id']}_{container['name']}",
                )
            except Exception as exc:
                print(f"[image] container failed: {container['name']} ({exc})", flush=True)
            if index % 10 == 0:
                print(f"[image] containers {index}/{len(containers)}", flush=True)
            scraper.sleep()

        for index, drop in enumerate(all_drops, start=1):
            try:
                drop["local_image_path"] = scraper.download_image(
                    drop["image_url"],
                    loot_image_dir,
                    f"{drop['grade']}_{drop['item_name']}",
                )
            except Exception as exc:
                print(f"[image] loot failed: {drop['item_name']} ({exc})", flush=True)
            if index % 50 == 0:
                print(f"[image] loot {index}/{len(all_drops)}", flush=True)
            scraper.sleep()

    output = {
        "source": {
            "site": BASE_URL.rstrip("/"),
            "entry_pages": [ENTRY_URL, CONTAINER_PAGE_URL],
            "app_version": scraper.app_version,
            "samples_per_container": config.samples_per_container,
            "note": "drop rates are estimates from simulator samples, not official static probabilities",
        },
        "containers": containers,
    }

    write_json(output, out_dir / "deltaforce_containers.json")
    write_containers_csv(containers, out_dir / "deltaforce_containers.csv")
    write_drops_csv(all_drops, out_dir / "deltaforce_container_drops.csv")

    print(f"Done. containers={len(containers)}, sampled_drops={len(all_drops)}", flush=True)
    print(f"JSON: {out_dir / 'deltaforce_containers.json'}", flush=True)
    print(f"CSV : {out_dir / 'deltaforce_containers.csv'}", flush=True)
    print(f"Drops CSV: {out_dir / 'deltaforce_container_drops.csv'}", flush=True)


if __name__ == "__main__":
    main()
