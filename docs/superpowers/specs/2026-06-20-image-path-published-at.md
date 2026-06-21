# 图片路径统一使用 `published_at` 日期 — 设计文档

> **状态:** 草稿  
> **日期:** 2026-06-20  
> **目标:** 图片上传路径和 Web 渲染路径统一使用文章 `published_at` 日期

## 当前状态

图片上传路径：**`format_date_folder()` → 当前日期**  
Web 渲染路径：**`updated_at` → 存入/更新时的 NOW()**

两边语义错误——图片路径应该反映**文章发布日**，而非抓取日。

## 核心设计

**路径在提取阶段全部算好，`download()` 只管下载和存储。**

```
_extract_image_urls(content)
  → {"https://x.com/a.jpg": "images/a.jpg"}

在 _run_batch_image_download 的 for 循环中拼接 date_str：
  → {"https://x.com/a.jpg": "news/2026-06-15/images/a.jpg"}

download(url_to_target, storage)
  → 下载每个 URL，直接存到给定的 target_path
  → 返回 {"https://x.com/a.jpg": "images/a.jpg"}  (content 替换用)
```

---

## 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `news/images.py` | `download()` 改为接受 `url_map: Dict[str, str]`，直接使用给定的 target_path 存储 |
| 2 | `news/images.py` | `_download_and_save()` 直接接受完整 S3 `target_path`，不再内部计算 |
| 3 | `news/images.py` | 删除 `_images_dir()` |
| 4 | `news/crawler.py` | `_extract_image_urls()` 改为实例方法，返回 `Dict[str, str]` |
| 5 | `news/crawler.py` | `_run_batch_image_download()` 新增可选 `date_str` 参数，for 循环中拼接 full path |
| 6 | `news/crawler.py` | `fetch()` 内联提取 `published_at` 日期并传入 |
| 7 | `web/app.py` | `_resolve_image_paths()` 用 `published_at` 替代 `updated_at` |

---

## 详细设计

### 1. `news/images.py` — `download()`

```python
def download(
    self, url_map: Dict[str, str], storage: FileStorage,
) -> Dict[str, str]:
    """Download images and save to pre-computed paths.

    Args:
        url_map: ``{url: "news/YYYY-MM-DD/images/xxx.jpg", ...}``
        storage: :class:`FileStorage` backend.

    Returns:
        ``{url: "images/xxx.jpg", ...}`` — content 替换用。
    """
    if not url_map:
        return {}
    result: Dict[str, str] = {url: "" for url in url_map}
    print(f"[ImageProcessor] Downloading {len(result)} unique images "
          f"(workers={self._max_workers})")
    executor = self._get_executor()
    futures = {
        executor.submit(self._download_and_save, url, target_path, storage): url
        for url, target_path in url_map.items()
    }
    for future in as_completed(futures):
        url = futures[future]
        try:
            saved_path = future.result()
            if saved_path:
                result[url] = saved_path
        except Exception as e:
            print(f"[ImageProcessor] Download failed [{url}]: {e}")
    success = sum(1 for v in result.values() if v)
    print(f"[ImageProcessor] Downloaded {success}/{len(result)} images")
    return result
```

### 2. `news/images.py` — `_download_and_save()`

```python
def _download_and_save(
    self, url: str, target_path: str, storage: FileStorage,
) -> Optional[str]:
    """Download *url* and save directly to *target_path* (full S3 key).

    Returns ``"images/xxx.jpg"`` on success, ``None`` on failure.
    """
    try:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        content_type = (
            resp.headers.get("Content-Type", "image/jpeg")
            .split(";")[0]
            .strip()
        )
    except requests.RequestException as e:
        print(f"[ImageProcessor] HTTP error for {url}: {e}")
        return None

    try:
        storage.save(resp.content, target_path, content_type)
    except Exception as e:
        print(f"[ImageProcessor] Save failed [{url}]: {e}")
        return None

    # 从完整 S3 路径提取 "images/xxx.jpg" 用于 content 替换
    # target_path = "news/2026-06-15/images/xxx.jpg"
    return target_path.split("news/", 1)[1] if "news/" in target_path else target_path
```

### 3. `news/images.py` — 删除 `_images_dir()`

不再需要。

### 4. `news/crawler.py` — `_extract_image_urls()`

```python
def _extract_image_urls(self, markdown: str) -> Dict[str, str]:
    """Extract image URLs and pre-compute relative paths.

    Returns ``{url: "images/xxx.jpg", ...}``.
    """
    urls: List[str] = []
    urls.extend(re.findall(r'!\[.*?\]\((https?://[^\s)]+)\)', markdown))
    urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', markdown, re.IGNORECASE))

    processor = self._get_image_processor()
    return {
        url: f"images/{processor._extract_filename(url, '.jpg')}"
        for url in urls
    }
```

### 5. `news/crawler.py` — `_run_batch_image_download()`

```python
def _run_batch_image_download(
    self,
    items: List[Dict[str, Any]],
    image_storage=None,
    date_str: Optional[str] = None,
) -> None:
    if image_storage is None:
        print("[Crawler] Phase 2 — S3 not configured, skipping image download")
        return

    from utils import format_date_folder
    day = date_str or format_date_folder()

    # 收集 url → full_s3_path，在 for 循环中拼接日期
    url_to_target: Dict[str, str] = {}
    for it in items:
        if it.get("content"):
            for url, rel_path in self._extract_image_urls(it["content"]).items():
                if url not in url_to_target:
                    url_to_target[url] = f"news/{day}/{rel_path}"

    if not url_to_target:
        print("[Crawler] Phase 2 — no images found, skipping")
        return

    print(f"[Crawler] Phase 2 — downloading {len(url_to_target)} unique images")
    processor = self._get_image_processor()
    url_map = processor.download(url_to_target, storage=image_storage)

    if not url_map:
        print("[Crawler] Phase 2 done (no images downloaded)")
        return

    # 替换 content 中的 URL（逻辑不变）
    replaced = 0
    for it in items:
        md = it.get("content", "")
        if not md:
            continue
        for old_url, new_path in url_map.items():
            if old_url in md:
                md = md.replace(old_url, new_path)
                replaced += 1
        it["content"] = md

    print(f"[Crawler] Phase 2 done: {replaced} replacements across "
          f"{sum(1 for it in items if it.get('content'))} articles")
```

### 6. `news/crawler.py` — `fetch()` 内联提取日期

```python
# fetch() 中，line 246：
if with_image:
    storage = self._resource_storage
    if target_storage:
        storage = target_storage
    # 从 published_at 提取日期，空值回退到当天
    pub = item.get("published_at", "")
    date_str = pub.strftime("%Y-%m-%d") if hasattr(pub, "strftime") else (str(pub)[:10] if pub else "")
    self._run_batch_image_download([item], storage, date_str=date_str or None)
```

`fetch_all()` 和 `sync_from_cloud()` **不改**——走 `enrich_content` → `_run_batch_image_download` 时不传 `date_str`，默认 `format_date_folder()`。

### 7. `web/app.py` — `_resolve_image_paths()` 改用 `published_at`

```python
# 改前
article["content"] = _resolve_image_paths(
    article["content"], article.get("updated_at"),
)

# 改后
article["content"] = _resolve_image_paths(
    article["content"], article.get("published_at"),
)
```

函数体不变。

---

## 影响范围

```
news/images.py          ← download() *urls → url_map: Dict[str, str]
                        ← _download_and_save() 接受完整 target_path
                        ← 删除 _images_dir()
news/crawler.py         ← _extract_image_urls() → 实例方法，返回 Dict[str, str]
                        ← _run_batch_image_download() 新增 date_str，for 循环拼接路径
                        ← fetch() 内联提取 published_at 日期传入
web/app.py              ← _resolve_image_paths() updated_at → published_at
```

**不改的文件：** `enrich_content()`、`fetch_all()`、`sync_from_cloud()`

## 数据流

```
fetch() 单篇:
  pub = item.get("published_at", "")
  date_str = pub.strftime(...) or None
  _run_batch_image_download([item], storage, date_str=date_str)
    _extract_image_urls(content) → {"https://x.com/a.jpg": "images/a.jpg"}
    for loop: url_to_target["https://x.com/a.jpg"] = "news/2026-06-15/images/a.jpg"
    download(url_to_target, storage)
      → S3: "news/2026-06-15/images/a.jpg"
      → return {"https://x.com/a.jpg": "images/a.jpg"}
    content: "images/a.jpg"

fetch_all() / sync_from_cloud():
  enrich_content(*items) → _run_batch_image_download(items, storage)
    date_str=None → day = format_date_folder()  # 当天
    ...同上...

Web 渲染:
  _resolve_image_paths(content, published_at="2026-06-15T10:30:00")
    → "images/a.jpg" → "/media/news/2026-06-15/images/a.jpg"
```
