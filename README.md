# DeltaForce Collection Scraper

从 `https://orzice.com/v/collection` 和 `https://orzice.com/v/scp_book` 抓取三角洲行动收集品数据，合并后导出 JSON/CSV，并默认下载图片。

## 安装依赖

```powershell
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 运行

```powershell
python .\scrape_deltaforce_collections.py
```

默认输出：

- `data/deltaforce_collections.json`
- `data/deltaforce_collections.csv`
- `data/images/`

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
