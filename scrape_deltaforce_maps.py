#!/usr/bin/env python3
"""Scrape Delta Force map resources from orzice.com.

The map page keeps map/mode metadata in front-end JavaScript and returns point
data from an encrypted API. This script parses the public page assets, asks a
small Node.js helper to reuse the site's token/decode functions, then writes
repeatable JSON/CSV outputs grouped by map and mode.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://orzice.com"
MAP_URL = BASE_URL + "/sjz/map/"
MAP_COMMON_JS = MAP_URL + "js/lib/map_common.js"
MAP_MAIN_JS = MAP_URL + "js/lib/main.js"
CRYPTO_JS_URL = BASE_URL + "/static/js/crypto-js.min.js"
TOKEN_JS_URL = BASE_URL + "/static/func/token.js"
DEFAULT_TILE_BASE = "https://game.gtimg.cn/images/dfm/cp/a20240729directory/img/"
DEFAULT_ICON_BASE = "https://game.gtimg.cn/images/dfm/cp/a20240729directory/img/lv3/"

POINT_CSV_FIELDS = [
    "id",
    "mid",
    "name",
    "sub_name",
    "category",
    "type",
    "type_label",
    "x",
    "y",
    "z",
    "floor",
    "grade",
    "tips",
    "diy",
    "run",
    "show",
    "collect",
    "random",
    "point1",
    "point2",
    "image_url",
    "local_image_path",
]

SPECIAL_UNIT_KEYWORDS = (
    "卫队",
    "首领",
    "机枪兵",
    "盾兵",
    "火箭兵",
    "喷火兵",
    "狙击手",
    "特殊兵",
)


@dataclass
class ScraperConfig:
    out_dir: Path
    delay: float
    timeout: float
    download_images: bool
    only_map: str | None
    only_level: str | None
    max_modes: int | None


class DeltaForceMapScraper:
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
                "Referer": MAP_URL,
            }
        )

    def fetch_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.config.timeout)
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    def scrape(self) -> list[dict[str, Any]]:
        common_js = self.fetch_text(MAP_COMMON_JS)
        main_js = self.fetch_text(MAP_MAIN_JS)

        item_types = parse_item_types(common_js)
        class_types = parse_class_types(common_js)
        map_infos = parse_map_infos(common_js)
        map_configs = parse_map_configs(main_js)
        map_configs = self.filter_configs(map_configs)

        print(f"[config] maps/modes={len(map_configs)}", flush=True)
        api_payloads = fetch_map_api_payloads(map_configs, timeout=self.config.timeout)
        api_by_code = {entry["code"]: entry for entry in api_payloads}

        all_modes: list[dict[str, Any]] = []
        type_lookup = build_type_lookup(item_types)
        for index, map_config in enumerate(map_configs, start=1):
            code = map_config["code"]
            api_entry = api_by_code.get(code)
            if not api_entry:
                print(f"[warn] no api payload for {code}", flush=True)
                continue

            payload = api_entry["data"]
            regions = payload.get("info") or []
            points = payload.get("item") or []
            map_info = map_infos.get(map_config.get("info_var", ""), {})
            mode_dir = self.mode_dir(map_config)
            image_dir = mode_dir / "images"
            if self.config.download_images:
                image_dir.mkdir(parents=True, exist_ok=True)

            enriched_points = [
                enrich_point(point, type_lookup, class_types)
                for point in points
                if isinstance(point, dict)
            ]
            if self.config.download_images:
                for image_index, point in enumerate(enriched_points, start=1):
                    try:
                        self.download_point_image(point, image_dir)
                    except Exception as exc:
                        print(f"[image] failed: {point.get('name')} ({exc})", flush=True)
                    if image_index % 100 == 0:
                        print(f"[image] {code}: checked {image_index}/{len(enriched_points)}", flush=True)
                    self.sleep()

            summary = summarize_points(enriched_points)
            mode_record = {
                "code": code,
                "map_id": map_config["map_id"],
                "mode_id": map_config["mode_id"],
                "map_name": map_config["map_name"],
                "mode_name": map_config["mode_name"],
                "layer": map_config["layer"],
                "map_info": map_info,
                "map_tile_template": build_tile_template(map_config, map_info),
                "api_path": api_entry.get("api_path", ""),
                "regions": regions,
                "points": enriched_points,
                "summary": summary,
                "source": {
                    "page": MAP_URL,
                    "api": BASE_URL + "/api/sjz/maps/get_item",
                    "map_common_js": MAP_COMMON_JS,
                    "map_main_js": MAP_MAIN_JS,
                },
            }

            mode_dir.mkdir(parents=True, exist_ok=True)
            write_json(mode_record, mode_dir / "metadata.json")
            write_json(enriched_points, mode_dir / "points.json")
            write_csv(enriched_points, mode_dir / "points.csv", POINT_CSV_FIELDS)
            all_modes.append(mode_record)

            print(
                f"[mode] {index}/{len(map_configs)} {map_config['map_name']} "
                f"{map_config['mode_name']}: points={len(enriched_points)}",
                flush=True,
            )
            self.sleep()

        aggregate = [
            {
                key: value
                for key, value in mode.items()
                if key not in {"points"}
            }
            | {"point_count": len(mode.get("points", []))}
            for mode in all_modes
        ]
        write_json(all_modes, self.config.out_dir / "deltaforce_maps_full.json")
        write_json(aggregate, self.config.out_dir / "deltaforce_maps.json")
        write_json({"item_types": item_types, "class_types": class_types}, self.config.out_dir / "deltaforce_map_types.json")
        return all_modes

    def filter_configs(self, configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = configs
        if self.config.only_map:
            needle = self.config.only_map.casefold()
            result = [
                config
                for config in result
                if needle in config["map_name"].casefold()
                or needle == str(config["map_id"])
                or needle == config["code"]
            ]
        if self.config.only_level:
            needle = self.config.only_level.casefold()
            result = [
                config
                for config in result
                if needle in config["mode_name"].casefold()
                or needle == str(config["mode_id"])
            ]
        if self.config.max_modes is not None:
            result = result[: self.config.max_modes]
        return result

    def mode_dir(self, map_config: dict[str, Any]) -> Path:
        map_dir = safe_filename(f"{map_config['map_id']}_{map_config['map_name']}")
        mode_dir = safe_filename(f"{map_config['mode_id']}_{map_config['mode_name']}")
        return self.config.out_dir / "modes" / map_dir / mode_dir

    def download_point_image(self, point: dict[str, Any], image_dir: Path) -> None:
        image_url = normalize_icon_url(point.get("icon") or point.get("image_url") or "")
        point["image_url"] = image_url
        if not image_url:
            return

        suffix = image_suffix(image_url)
        filename = safe_filename(f"{point.get('id', '')}_{point.get('name', '')}_{point.get('type', '')}") + suffix
        target = image_dir / filename
        point["local_image_path"] = str(target.as_posix())
        if target.exists() and target.stat().st_size > 0:
            return

        response = self.session.get(image_url, timeout=self.config.timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if target.suffix == ".img":
            detected = suffix_from_content_type(content_type)
            if detected != ".img":
                target = target.with_suffix(detected)
                point["local_image_path"] = str(target.as_posix())
        target.write_bytes(response.content)

    def sleep(self) -> None:
        if self.config.delay > 0:
            time.sleep(self.config.delay)


def parse_item_types(common_js: str) -> list[dict[str, Any]]:
    block = extract_assignment_block(common_js, "var item_types")
    types: list[dict[str, Any]] = []
    for entry in re.finditer(r"\{(.*?)\}", block, re.S):
        body = entry.group(1)
        name = extract_js_field(body, "name")
        id_type = parse_int(extract_js_field(body, "idType"))
        icon = extract_js_field(body, "icon")
        type_group = extract_js_field(body, "types")
        if name is None and id_type is None and icon is None:
            continue
        types.append(
            {
                "idType": id_type,
                "name": name or "",
                "types": type_group or "wzd",
                "icon": icon or "",
            }
        )
    return types


def parse_class_types(common_js: str) -> dict[str, str]:
    block = extract_assignment_block(common_js, "var class_types")
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]", block)
    }


def parse_map_infos(common_js: str) -> dict[str, dict[str, Any]]:
    infos: dict[str, dict[str, Any]] = {}
    for match in re.finditer(r"var\s+(\w+Info)\s*=", common_js):
        name = match.group(1)
        brace_start = common_js.find("{", match.end())
        semicolon_start = common_js.find(";", match.end())
        if brace_start < 0 or (semicolon_start != -1 and semicolon_start < brace_start):
            continue
        block = extract_brace_block(common_js, brace_start)
        info: dict[str, Any] = {}
        for key, raw in re.findall(r"(\w+)\s*:\s*([^,\n]+)", block):
            raw = raw.strip().strip("'\"")
            if raw.startswith("[") or raw.startswith("{"):
                continue
            value: Any = parse_float(raw)
            if value is None:
                value = raw
            info[key] = value
        infos[name] = info
    return infos


def parse_map_configs(main_js: str) -> list[dict[str, Any]]:
    block = extract_assignment_block(main_js, "const mapConfigs")
    configs: list[dict[str, Any]] = []
    for entry in re.finditer(r"['\"]([^'\"]+)['\"]\s*:\s*\{(.*?)\n\s*\}", block, re.S):
        code = entry.group(1)
        body = entry.group(2)
        map_id = parse_int(extract_js_field(body, "id"))
        mode_id = parse_int(extract_js_field(body, "lv"))
        map_name = extract_js_field(body, "name")
        mode_name = extract_js_field(body, "level")
        layer = extract_js_field(body, "layer")
        info_var = extract_js_field(body, "info")
        if map_id is None or mode_id is None or not map_name or not mode_name:
            continue
        configs.append(
            {
                "code": code,
                "map_id": map_id,
                "mode_id": mode_id,
                "map_name": map_name,
                "mode_name": mode_name,
                "layer": layer or "",
                "info_var": info_var or "",
            }
        )
    return configs


def extract_assignment_block(text: str, assignment: str) -> str:
    start = text.find(assignment)
    if start < 0:
        raise ValueError(f"Cannot find JavaScript assignment: {assignment}")
    brace = text.find("{", start)
    bracket = text.find("[", start)
    if bracket != -1 and bracket < brace:
        return extract_bracket_block(text, bracket)
    return extract_brace_block(text, brace)


def extract_brace_block(text: str, start: int) -> str:
    return extract_balanced_block(text, start, "{", "}")


def extract_bracket_block(text: str, start: int) -> str:
    return extract_balanced_block(text, start, "[", "]")


def extract_balanced_block(text: str, start: int, open_char: str, close_char: str) -> str:
    if start < 0 or text[start] != open_char:
        raise ValueError("Invalid block start")
    depth = 0
    quote = ""
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ValueError("Unclosed JavaScript block")


def extract_js_field(body: str, field: str) -> str | None:
    pattern = rf"(?:['\"]{re.escape(field)}['\"]|\b{re.escape(field)}\b)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|(-?\d+(?:\.\d+)?)|([A-Za-z_]\w*))"
    match = re.search(pattern, body)
    if not match:
        return None
    return next((group for group in match.groups() if group is not None), None)


def fetch_map_api_payloads(configs: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    if not shutil.which("node"):
        raise RuntimeError("Node.js is required to decode the Orzice map API response.")

    helper_input = {
        "baseUrl": BASE_URL,
        "mapUrl": MAP_URL,
        "cryptoJsUrl": CRYPTO_JS_URL,
        "tokenJsUrl": TOKEN_JS_URL,
        "configs": configs,
    }
    completed = subprocess.run(
        ["node", "-e", NODE_HELPER],
        input=json.dumps(helper_input, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=max(timeout * max(len(configs), 1) + 30, 60),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Node API helper failed.\n"
            f"STDOUT: {completed.stdout[-2000:]}\n"
            f"STDERR: {completed.stderr[-2000:]}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse Node helper output: {completed.stdout[:2000]}") from exc
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result.get("results", [])


NODE_HELPER = r"""
const fs = require('fs');
const vm = require('vm');

function write(value) {
  process.stdout.write(JSON.stringify(value));
}

(async () => {
  try {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    global.window = global;
    global.document = {
      cookie: '',
      domain: 'orzice.com',
      referrer: input.mapUrl,
      documentElement: {},
      addEventListener() {},
      getElementById() { return null; }
    };
    global.location = {
      href: input.mapUrl,
      host: 'orzice.com',
      hostname: 'orzice.com',
      pathname: '/sjz/map/',
      search: '',
      protocol: 'https:',
      origin: input.baseUrl
    };
    global.navigator = { userAgent: 'Mozilla/5.0', language: 'zh-CN' };
    global.TimeUnix = Math.round(Date.now() / 1000);
    global.TimeUnixBD = global.TimeUnix;
    global.TimeUnixDiff = 0;
    global.GetTimeUnix = () => Math.round(Date.now() / 1000);

    const cryptoText = await (await fetch(input.cryptoJsUrl, { headers: { referer: input.mapUrl } })).text();
    const sandbox = { module: { exports: {} }, exports: {}, window: global, global: global };
    vm.createContext(sandbox);
    vm.runInContext(cryptoText, sandbox, { filename: input.cryptoJsUrl });
    global.CryptoJS = sandbox.module.exports || sandbox.CryptoJS || sandbox.window.CryptoJS;
    if (!global.CryptoJS) throw new Error('CryptoJS was not loaded');

    const tokenText = await (await fetch(input.tokenJsUrl, { headers: { referer: input.mapUrl } })).text();
    vm.runInThisContext(tokenText, { filename: input.tokenJsUrl });
    if (typeof GetPath !== 'function' || typeof GetData01 !== 'function') {
      throw new Error('Orzice token/decode functions were not loaded');
    }

    const results = [];
    for (const config of input.configs) {
      const query = `id=${config.map_id}&lv=${config.mode_id}`;
      const apiPath = '/api/sjz/maps/get_item' + GetPath(query);
      const response = await fetch(input.baseUrl + apiPath, {
        headers: {
          'user-agent': 'Mozilla/5.0',
          referer: input.mapUrl,
          accept: 'application/json'
        }
      });
      if (!response.ok) throw new Error(`API ${apiPath} failed with ${response.status}`);
      const body = await response.json();
      if (body.code !== 0) throw new Error(`API ${apiPath} returned code ${body.code}: ${body.message || body.msg || ''}`);
      results.push({
        code: config.code,
        api_path: apiPath,
        data: GetData01(body.data)
      });
    }
    write({ results });
    process.exit(0);
  } catch (error) {
    write({ error: error && error.stack ? error.stack : String(error) });
    process.exit(1);
  }
})();
"""


def build_type_lookup(item_types: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for item_type in item_types:
        id_type = item_type.get("idType")
        type_group = item_type.get("types")
        if id_type is not None:
            lookup[str(id_type)] = item_type
        if type_group:
            lookup.setdefault(str(type_group), item_type)
    return lookup


def enrich_point(
    point: dict[str, Any],
    type_lookup: dict[str, dict[str, Any]],
    class_types: dict[str, str],
) -> dict[str, Any]:
    result = point.copy()
    point_type = str(result.get("type") or "")
    type_info = type_lookup.get(point_type, {})
    type_group = str(type_info.get("types") or point_type or "wzd")
    if point_type in class_types:
        type_group = point_type

    result["type_label"] = class_types.get(point_type) or type_info.get("name") or point_type
    result["category"] = normalize_category(type_group, result)
    result["image_url"] = normalize_icon_url(str(result.get("icon") or ""))
    result.setdefault("local_image_path", "")
    result["position"] = {
        "x": parse_float(result.get("x")),
        "y": parse_float(result.get("y")),
        "z": result.get("z") or "",
        "floor": parse_int(result.get("floor")),
    }
    return result


def normalize_category(type_group: str, point: dict[str, Any]) -> str:
    if type_group == "Boss":
        return "boss"
    if type_group == "key":
        return "key_room"
    if type_group == "hong":
        return "rare_card_spawn"
    if type_group == "revive":
        return "spawn"
    if type_group == "retreat":
        return "extraction"
    if type_group == "hd":
        return "activity"
    text = " ".join(str(point.get(key) or "") for key in ("name", "sub_name", "tips", "diy"))
    if any(keyword in text for keyword in SPECIAL_UNIT_KEYWORDS):
        return "special_unit"
    return "loot_point"


def summarize_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for point in points:
        by_category[point["category"]] = by_category.get(point["category"], 0) + 1
        type_label = str(point.get("type_label") or point.get("type") or "")
        by_type[type_label] = by_type.get(type_label, 0) + 1
    return {
        "total_points": len(points),
        "by_category": dict(sorted(by_category.items())),
        "by_type": dict(sorted(by_type.items())),
        "bosses": compact_points(points, "boss"),
        "special_units": compact_points(points, "special_unit"),
        "key_rooms": compact_points(points, "key_room"),
        "rare_card_spawns": compact_points(points, "rare_card_spawn"),
    }


def compact_points(points: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for point in points:
        if point.get("category") != category:
            continue
        compacted.append(
            {
                "id": point.get("id"),
                "name": point.get("name"),
                "sub_name": point.get("sub_name"),
                "type": point.get("type"),
                "type_label": point.get("type_label"),
                "position": point.get("position"),
                "tips": point.get("tips"),
                "diy": point.get("diy"),
                "collect": point.get("collect"),
                "image_url": point.get("image_url"),
                "local_image_path": point.get("local_image_path"),
            }
        )
    return compacted


def build_tile_template(map_config: dict[str, Any], map_info: dict[str, Any]) -> str:
    href = str(map_info.get("href") or DEFAULT_TILE_BASE)
    return urljoin(href, f"{map_config['layer']}/{{z}}_{{x}}_{{y}}.jpg")


def normalize_icon_url(icon: str) -> str:
    icon = icon.strip()
    if not icon:
        return ""
    if icon.startswith("//"):
        return "https:" + icon
    if icon.startswith("http://") or icon.startswith("https://"):
        return icon
    return urljoin(DEFAULT_ICON_BASE, icon + ".png")


def write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip("._ ")
    return value[:150] or "unnamed"


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


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d+", str(value))
    return int(match.group(0)) if match else None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Delta Force map resources from orzice.com.")
    parser.add_argument("--out-dir", default="data/maps", help="Output directory. Default: data/maps")
    parser.add_argument("--delay", type=float, default=0.05, help="Delay between image requests in seconds. Default: 0.05")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds. Default: 30")
    parser.add_argument("--skip-images", action="store_true", help="Do not download marker images.")
    parser.add_argument("--only-map", default=None, help="Filter by map name, map id, or config code.")
    parser.add_argument("--only-level", default=None, help="Filter by mode name or mode id.")
    parser.add_argument("--max-modes", type=int, default=None, help="Debug option: limit number of map modes.")
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
        only_map=args.only_map,
        only_level=args.only_level,
        max_modes=args.max_modes,
    )
    scraper = DeltaForceMapScraper(config)
    modes = scraper.scrape()
    print(f"Done. modes={len(modes)}", flush=True)
    print(f"JSON: {out_dir / 'deltaforce_maps.json'}", flush=True)
    print(f"Full JSON: {out_dir / 'deltaforce_maps_full.json'}", flush=True)
    print(f"Mode files: {out_dir / 'modes'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
