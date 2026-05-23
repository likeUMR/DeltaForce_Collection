# DeltaForce Data Scrapers

从 `https://orzice.com/v/collection` 和 `https://orzice.com/v/scp_book` 抓取三角洲行动收集品数据，合并后导出 JSON/CSV，并默认下载图片。

也支持从 `https://www.kkrb.net/?viewpage=view%2Fmap%2Fkeycard_room` 抓取钥匙卡/钥匙房数据，包括钥匙卡图片、名称、价格、容器信息、使用地点、耐久与不同难度单次开门成本。

## 安装依赖

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 运行

```powershell
python .\scrape_deltaforce_collections.py
```

默认输出：

- `data/collections/deltaforce_collections.json`
- `data/collections/deltaforce_collections.csv`
- `data/collections/images/`

常用参数：

```powershell
# 只抓元数据，不下载图片
python .\scrape_deltaforce_collections.py --skip-images

# 指定输出目录，并调慢请求
python .\scrape_deltaforce_collections.py --out-dir .\output --delay 0.5

# 调试时每个分类只抓 1 页
python .\scrape_deltaforce_collections.py --max-pages 1 --skip-images
```

字段包括：名称、图片链接、本地图片路径、稀有度等级、分类、交易状态、当前价格、3 日价格、7 日价格、30 日价格及对应涨跌幅。未登录无法看到的实时当前价会留空。

## 地图资源爬取

地图工具页的数据来自前端配置和加密 API，脚本会按“地图 / 模式”分别保存点位、Boss、特殊兵种、钥匙房、刷红点、出生点和撤离点等信息。解密步骤会复用站点前端脚本，因此需要本机已安装 Node.js。

```powershell
python .\scrape_deltaforce_maps.py
```

默认输出：

- `data/maps/deltaforce_maps.json`
- `data/maps/deltaforce_maps_full.json`
- `data/maps/deltaforce_map_types.json`
- `data/maps/modes/<地图>/<模式>/metadata.json`
- `data/maps/modes/<地图>/<模式>/points.json`
- `data/maps/modes/<地图>/<模式>/points.csv`
- `data/maps/modes/<地图>/<模式>/images/`

常用参数：

```powershell
# 只抓元数据，不下载点位图标
python .\scrape_deltaforce_maps.py --skip-images

# 调试时只抓一个地图模式
python .\scrape_deltaforce_maps.py --skip-images --max-modes 1

# 只抓指定地图或难度
python .\scrape_deltaforce_maps.py --only-map 零号大坝 --only-level 机密
```

## 容器资源爬取

容器脚本从 KK日报/三角洲一图流的容器页面接口抓取数据：

- `getCIEData`：容器名称、图片、主要产物类型、必出/可能/不出货格数等。
- `getLCSData`：模拟容器搜索列表，以及模拟搜索产出。

```powershell
python .\scrape_deltaforce_containers.py
```

默认输出：

- `data/containers/deltaforce_containers.json`
- `data/containers/deltaforce_containers.csv`
- `data/containers/deltaforce_container_drops.csv`
- `data/containers/images/containers/`
- `data/containers/images/loot/`

常用参数：

```powershell
# 只抓基础容器信息，不跑模拟搜索
python .\scrape_deltaforce_containers.py --samples-per-container 0 --skip-images

# 每个支持模拟的容器采样 1000 次，用于更稳定地估算爆率
python .\scrape_deltaforce_containers.py --samples-per-container 1000 --draw-batch-size 100

# 只抓指定容器
python .\scrape_deltaforce_containers.py --container-id bxg --samples-per-container 300

# 尝试对容器一览中的所有容器请求模拟接口
python .\scrape_deltaforce_containers.py --try-all-simulation
```

说明：网页没有直接公开静态“官方爆率表”。脚本中的 `grade_drop_rates`、`occurrences_per_draw`、`draw_hit_rate` 是基于模拟搜索接口采样得到的估算值；样本越大越稳定。

## 抓取钥匙卡信息

```powershell
python .\scrape_deltaforce_keycards.py
```

默认输出：

- `data/keycards/deltaforce_keycards.json`
- `data/keycards/deltaforce_keycards.csv`
- `data/keycards/images/keycards/`
- `data/keycards/images/containers/`

常用参数：

```powershell
# 只抓元数据，不下载图片
python .\scrape_deltaforce_keycards.py --skip-images

# 指定输出目录，并调慢图片请求
python .\scrape_deltaforce_keycards.py --out-dir .\output --delay 0.5
```

钥匙卡脚本会自动完成页面 Cookie 初始化、菜单版本获取、UA 状态检查和数据接口请求，因此可以重复运行刷新最新价格与容器数据。
