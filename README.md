# TMM-Lite

轻量级影视媒体刮削小程序 — tinyMediaManager (TMM) 的极简自建版。

自动扫描本地电影/剧集目录，从 TMDB + 豆瓣拉取元数据和海报，写入 Kodi/Jellyfin/Emby 兼容的 NFO 文件，提供 Web 管理界面。

## 主要功能

- **自动刮削**：定时扫描媒体库，从 TMDB（主）+ 豆瓣（辅）拉取元数据、海报、背景图
- **Kodi 兼容**：生成标准 `movie.nfo` / `tvshow.nfo`，Jellyfin / Emby / Kodi 直接可用
- **影视库预览**：海报网格展示，按分类筛选，点击查看详情及播放
- **在线播放**：浏览器内 HTML5 视频播放，支持 MP4/MKV/WebM 及拖拽进度条
- **PotPlayer 联动**：一键复制流地址，Ctrl+U 粘贴即可在 PotPlayer 中播放
- **字幕管理**：单条/批量下载字幕，自动检测已有中文字幕，一键刷新非中文标题条目
- **合集识别**：自动识别并跳过合集/套装目录（如 "Harry Potter 8-Film Collection"），仅刮削单部电影
- **PTer/FRDS 命名兼容**：自动提取括号年份后的英文标题，提高 TMDB 匹配率
- **Web 管理**：仪表盘、条目列表、实时日志、设置，纯服务端渲染无前端构建链

## 快速开始

### 1. 申请 TMDB API Key

访问 [themoviedb.org](https://www.themoviedb.org/) 注册账号，在 [API 设置页](https://www.themoviedb.org/settings/api) 申请 API Key（免费）。

### 2. 准备媒体目录

按以下约定组织你的媒体文件：

```
/movies/                          # 电影目录
  星际穿越 (2014)/
    Interstellar.2014.1080p.mkv
  流浪地球2 (2023)/
    The.Wandering.Earth.2.mkv

/tvshows/                         # 剧集目录
  繁花 (2023)/
    Season 01/
      繁花.S01E01.mkv
```

**命名规范：**
- 电影：一个电影一个文件夹，命名建议 `片名 (年份)`
- 剧集：一部剧一个文件夹，命名建议 `剧名 (年份)`，内含 `Season 01`/`S01` 子文件夹

### 3. 配置并启动

编辑 `docker-compose.yml`，将 `/path/to/movies` 和 `/path/to/tvshows` 改为你的实际路径（可选：填入 TMDB API Key 环境变量）：

```bash
docker compose up -d
```

打开浏览器访问 `http://<你的IP>:8000/`。

### 4. 使用

1. **设置页** — 填入 TMDB API Key（如在 docker-compose 中已设置环境变量则可跳过）；按需开启豆瓣、字幕下载、调整定时任务时间
2. **媒体库页** — 添加你的电影/剧集目录（容器内路径，即 compose 中 volumes 右侧的路径）
3. **仪表盘** — 点击「立即执行一次」开始刮削，或等待定时任务自动执行
4. **条目页** — 查看刮削状态，对失败的条目可单独「重新刮削」；可一键刷新非中文标题条目的字幕
5. **影视库** — 海报网格浏览已匹配的媒体，按分类筛选，点击海报查看详情、复制路径或跳转在线播放
6. **在线播放** — 点击「在线播放」在浏览器中直接观看；点击「PotPlayer 播放」复制流地址后在 PotPlayer 中 Ctrl+U 打开

### 5. 网络代理（国内无法直连 TMDB 时）

若刮削条目状态显示 `failed`，错误为 `TMDB 网络错误: ConnectError: /search/movie`，说明无法直连 TMDB。需配置代理：

1. 在 **设置页 → 网络代理** 填入代理地址（如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`），点击保存
2. 回到仪表盘重新执行一次刮削

**注意（Docker 部署时）：** 容器默认走桥接网络，访问不到宿主机上只监听 `127.0.0.1` 的代理（如 Clash 默认 7890 端口）。请参照 `docker-compose.yml` 末尾的注释，改用 host 网络（设置页填 `http://127.0.0.1:7890`），或让代理监听 `0.0.0.0` 并在设置页填 `http://host.docker.internal:7890`。

代理为空时按直连处理，不影响原有功能。

## 安全提示

**本应用无内置认证，仅限内网使用，切勿直接暴露到公网。** 如需外网访问，请置于反向代理（Nginx/Caddy）之后并自行添加认证（Basic Auth / VPN）。

v1.0 容器以 root 运行以简化 NAS 写权限配置，请确认你的使用环境可接受此风险。

## 开发

```bash
pip install -r requirements-dev.txt
ruff check app tests    # 代码检查
mypy app                # 类型检查
pytest                  # 运行所有测试
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 技术栈

Python 3.12 + FastAPI + SQLAlchemy (SQLite) + APScheduler + httpx + Jinja2 + Docker
