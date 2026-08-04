# TMM-Lite 实施规格书 v1.0（修订版）

> 三份文档的关系：
> - `tmm-lite-design-spec.md` —— 做什么（需求与架构）
> - `tmm-lite-dev-plan.md` —— 怎么拆、验收到什么程度（模块划分与测试要求）
> - **本文档** —— 精确到可直接照写实现的规格：依赖版本、DDL、算法步骤、接口契约、
>   页面表单字段、错误文案、部署文件全文、测试夹具与上线执行手册。
>
> 冲突裁决顺序：本文档 > 开发文档 > 设计说明书（发现冲突需回写修订上游文档）。

---

## 1. 交付物清单（上线时必须齐备）

| # | 交付物 | 验收方式 |
|---|---|---|
| 1 | 可运行源码（目录结构见 §2） | CI 全绿 |
| 2 | 测试套件（单元 + 集成 + E2E） | `pytest` 通过，覆盖率达标（M3/M7 ≥85%，整体 ≥75%） |
| 3 | `Dockerfile` + `docker-compose.yml`（§12 全文） | §14.2 部署验收通过 |
| 4 | `README.md`（快速开始、Key 申请、目录约定、安全提示） | 按 README 从零部署成功 |
| 5 | 真实环境冒烟记录（§14.3） | 4 项全过，留存日志/截图 |

---

## 2. 项目骨架与依赖（固定，不再商议）

### 2.1 完整目录结构

```
tmm-lite/
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── pyproject.toml            # ruff + mypy + pytest 配置
├── scripts/
│   └── check_coverage.py      # 检查指定模块覆盖率门槛
├── data/                     # 运行时生成，git 忽略
│   ├── config.yaml
│   └── tmm-lite.db
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI 应用与装配（§11）
│   ├── config.py             # M1（§3）
│   ├── database.py           # M2（§4）
│   ├── exceptions.py         # 统一异常（§13.1）
│   ├── scanner.py            # M7（§9）
│   ├── scheduler.py          # M8（§10）
│   ├── nfo_writer.py         # M4（§6）
│   ├── parsers/
│   │   ├── __init__.py
│   │   └── filename_parser.py  # M3（§5）
│   ├── scrapers/
│   │   ├── __init__.py
│   │   ├── base.py           # ScrapedMeta 等共享数据类
│   │   ├── tmdb.py           # M5（§7）
│   │   └── douban.py         # M6（§8）
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── libraries.html
│   │   ├── items.html
│   │   ├── logs.html
│   │   └── settings.html
│   └── static/
│       └── style.css
└── tests/
    ├── conftest.py           # 公共夹具（§13.4）
    ├── fixtures/
    │   ├── tmdb_search_movie.json
    │   ├── tmdb_movie_detail.json
    │   ├── tmdb_search_tv.json
    │   ├── tmdb_tv_detail.json
    │   ├── douban_suggest.json
    │   └── douban_subject.html
    ├── test_config.py
    ├── test_database.py
    ├── test_filename_parser.py
    ├── test_nfo_writer.py
    ├── test_tmdb.py
    ├── test_douban.py
    ├── test_scanner.py
    ├── test_scheduler.py
    ├── test_web.py
    └── test_e2e.py
```

### 2.2 依赖（`requirements.txt`）

```
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
sqlalchemy>=2.0,<3
apscheduler>=3.10,<4
httpx>=0.27,<1
jinja2>=3.1,<4
pyyaml>=6.0,<7
lxml>=5.2,<6
python-multipart>=0.0.9        # FastAPI 表单解析必需
```

`requirements-dev.txt`：

```
-r requirements.txt
pytest>=8
pytest-asyncio>=0.23
respx>=0.21
pytest-cov>=5
ruff>=0.5
mypy>=1.10
```

### 2.3 全局常量（`app/__init__.py` 或各模块顶部）

```python
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov",
                    ".wmv", ".flv", ".rmvb", ".iso", ".mpg", ".mpeg"}

def get_data_dir() -> Path:
    # 调用时读取，避免测试导入模块后再设置环境变量却不生效。
    return Path(os.environ.get("TMM_DATA_DIR", "data"))
```

配置和数据库路径在应用装配时由 `data_dir / "config.yaml"`、`data_dir / "tmm-lite.db"` 得出，
不得作为函数默认参数在模块导入时冻结。测试优先通过 `create_app(data_dir=...)` 显式注入临时目录。

---

## 3. M1 配置管理 —— 精确规格

### 3.1 config.yaml 完整 Schema

| 键 | 类型 | 默认值 | 校验规则 |
|---|---|---|---|
| `tmdb_api_key` | str | `""` | 无（空表示回退环境变量） |
| `use_douban` | bool | `true` | — |
| `douban_delay_seconds` | float | `2.0` | 必须有限且 ≥ 0.5；NaN/正负无穷均拒绝 |
| `overwrite_existing_nfo` | bool | `false` | — |
| `language` | str | `"zh-CN"` | 非空 |
| `schedule_cron` | str | `"0 4 * * *"` | 必须恰好 5 段，且 `CronTrigger.from_crontab()` 可解析 |
| `libraries` | list | `[]` | 每项含 name/path/type，type ∈ {movie, tv}；仅首次导入用 |

### 3.2 行为矩阵

| 场景 | 行为 |
|---|---|
| 文件不存在 | 用默认值创建文件（含注释可省略），返回默认配置 |
| 文件存在但缺字段 | 缺失字段取默认值；**加载后不回写**（保持用户文件原样） |
| 文件含未知字段 | 保留在内存 `_extra` 中，`save_config` 时原样写回 |
| YAML 语法错误 | 抛 `ConfigError("config.yaml 格式错误: <原因>")`，**不覆盖文件**，进程启动失败（fail-fast） |
| YAML 根节点或字段类型错误 | 抛 `ConfigError("config.yaml 配置无效: <原因>")`，不做字符串/布尔值隐式转换，不覆盖文件 |
| `save_config` | 合并 updates → 校验（§3.1 规则）→ 写 `config.yaml.tmp` → `os.replace(tmp, target)` 原子替换；失败清理 tmp |
| API Key 解析 | `effective_tmdb_api_key = yaml值 or os.environ.get("TMDB_API_KEY", "")` |

### 3.3 接口（最终版签名）

```python
from app.exceptions import ConfigError

@dataclass
class LibrarySeed:
    name: str
    path: str
    type: str          # "movie" | "tv"

@dataclass
class AppConfig:
    tmdb_api_key: str              # yaml 原始值（可能为空）
    use_douban: bool
    douban_delay_seconds: float
    overwrite_existing_nfo: bool
    language: str
    schedule_cron: str
    libraries_seed: list[LibrarySeed]
    _extra: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def effective_tmdb_api_key(self) -> str: ...   # §3.2 优先级

def validate_cron(cron: str) -> CronTrigger
def load_config(path: Path | None = None) -> AppConfig
def save_config(updates: dict, path: Path | None = None) -> AppConfig
```

`path=None` 时在调用时用 `get_data_dir() / "config.yaml"`。`validate_cron` 是配置保存、应用启动和
调度器重排任务共用的唯一解析入口：先检查 `len(cron.split()) == 5`，再调用
`CronTrigger.from_crontab(cron)`，失败统一转成 `ConfigError("Cron 表达式无效")`。

`save_config` 可接受的 updates 键：`tmdb_api_key / use_douban / douban_delay_seconds / overwrite_existing_nfo / language / schedule_cron`。传入其他键抛 `ConfigError`。**不允许通过 save_config 修改 libraries**（单一事实来源：库走 DB）。

配置写入由 Web 层的应用级 `asyncio.Lock` 串行化；固定 `.tmp` 文件不能单独承担并发控制。
YAML 必须使用 `yaml.safe_load` / `yaml.safe_dump`。

---

## 4. M2 数据层 —— DDL 级定义

### 4.1 表结构（SQLAlchemy 2.0 Declarative，等价 DDL 如下）

```sql
CREATE TABLE library (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          VARCHAR(100) NOT NULL,
    path          VARCHAR(500) NOT NULL UNIQUE,
    media_type    VARCHAR(10)  NOT NULL CHECK (media_type IN ('movie','tv'))
);

CREATE TABLE media_item (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id     INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    media_type     VARCHAR(10) NOT NULL CHECK (media_type IN ('movie','tv')),
    folder_path    VARCHAR(1000) NOT NULL UNIQUE,
    parsed_title   VARCHAR(500),
    parsed_year    INTEGER,
    status         VARCHAR(20) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','matched','failed','manual_needed','missing')),
    source         VARCHAR(20),          -- v1 固定为 'tmdb'
    source_id      VARCHAR(50),
    imdb_id        VARCHAR(20),          -- 用于字幕精确匹配（TTMDB detail.imdb_id）
    matched_title  VARCHAR(500),
    matched_original_title VARCHAR(500),
    matched_year   INTEGER,
    overview       TEXT,
    rating         REAL,
    poster_url     VARCHAR(1000),
    backdrop_url   VARCHAR(1000),
    genres         VARCHAR(500),          -- 逗号分隔
    last_scraped_at DATETIME,             -- UTC
    error_message  TEXT
);
CREATE INDEX ix_media_item_status ON media_item(status);
CREATE INDEX ix_media_item_library ON media_item(library_id);

CREATE TABLE scrape_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   DATETIME NOT NULL,      -- UTC
    finished_at  DATETIME,
    total        INTEGER NOT NULL DEFAULT 0,
    matched      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);

CREATE TABLE app_meta (
    key          VARCHAR(100) PRIMARY KEY,
    value        TEXT NOT NULL
);
```

### 4.2 实现要求

- SQLite 连接必须开启外键：`PRAGMA foreign_keys=ON`（每连接 event listener），否则级联删除失效。
- engine 参数：`connect_args={"check_same_thread": False}`（FastAPI + 后台任务共用）。
- `init_db(db_path)`：`Base.metadata.create_all`，幂等，返回 `Engine`。随后执行轻量迁移 `_migrate`：用 `PRAGMA table_info` 检查旧库缺列（如 `imdb_id`），缺则 `ALTER TABLE ADD COLUMN`。
- `create_session_factory(engine)`：返回 `sessionmaker(bind=engine, expire_on_commit=False)`。
- Runner 和 FastAPI 依赖均使用 `with session_factory() as session:`，不得泄漏 Session；Web 层的
  `get_session` 用 yield 模式确保关闭。
- 时间统一 `datetime.now(timezone.utc)` 写入；模板过滤器 `localtime` 负责转本地展示。
- SQLite 读出的无时区 `datetime` 一律按 UTC 解释后再转本地时间。
- 测试若使用 `sqlite://`，必须配 `StaticPool`；也可直接使用 `tmp_path` 下的文件数据库。

最终接口：

```python
def init_db(db_path: Path) -> Engine
def create_session_factory(engine: Engine) -> sessionmaker[Session]
```

`AppMeta(key="libraries_seed_imported")` 用于记录初始化媒体库是否已经处理。不能仅用
“Library 表当前为空”判断首次启动，否则用户删除全部库后重启会再次导入 YAML seed。

---

## 5. M3 文件名解析器 —— 算法级规格

共享返回类型：

```python
@dataclass(frozen=True)
class ParsedName:
    title: str | None
    year: int | None
    season: int | None
    episode: int | None

def parse_folder_name(name: str) -> ParsedName
def parse_episode_name(name: str) -> ParsedName
```

### 5.1 噪音词表（模块常量，初始版本，全部大小写不敏感）

```python
NOISE_WORDS = {
    # 分辨率/画质
    "1080p", "720p", "2160p", "4k", "uhd", "hdr", "hdr10", "hdr10+", "dv", "dovi",
    "10bit", "8bit", "sdr",
    # 来源
    "bluray", "blu-ray", "bdrip", "brrip", "web-dl", "webdl", "webrip", "hdtv",
    "dvdrip", "remux", "hdrip", "cam", "ts",
    # 编码/音频
    "x264", "x265", "h264", "h.264", "h265", "h.265", "hevc", "avc", "av1",
    "aac", "ac3", "dts", "dts-hd", "truehd", "atmos", "ddp5.1", "dd5.1", "flac", "2audio",
    # 中文标签
    "国语", "粤语", "国粤双语", "国语中字", "中字", "中英字幕", "简繁", "双语", "高清", "蓝光",
    "完整版", "未删减", "修复版", "重制版", "特效字幕",
}
```

补充规则：末尾 `-XXX` 形式的发布组标签（连字符后无空格接大写字母/数字串直到结尾）整段删除，如 `-CMCT`、`-FRDS`。

### 5.2 `parse_folder_name(name)` 算法（严格按序执行）

1. `work = name.strip()`；空串 → 全 None 返回。
2. 用 `Path(work).suffix.casefold()` 检查扩展名；若 suffix 在 VIDEO_EXTENSIONS 中，从 work 尾部删除。
3. **从完整 work 提取季/集**（不能先截断年份后的内容）：
   - `[Ss](\d{1,2})[Ee](\d{1,3})` → season+episode；
   - 仅季：`Season[ ._]?(\d{1,2})` 或独立 `[Ss](\d{1,2})`（后无 E）；
   - 仅集：`第(\d{1,4})[集话]`。
4. **提取年份**：
   a. 正则 `[（(](?P<year>(?:19|20)\d{2})[)）]` 找**括号包裹**的年份（支持全角括号）→ 命中则取第一个，读取命名组 `year` 并记录位置。
   b. 未命中 → 正则 `(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)` 找裸年份，取**最后一个** Match 并记录位置。
   c. 都未命中 → year=None。
5. **确定标题区**：若年份来自括号 → 标题=括号前的子串；若来自裸年份 → 标题=第 4 步所选中 Match 的准确 `start()` 之前的子串。若该子串清洗后为空，则用全串继续，防止纯年份片名退化为空。
6. 将第 3 步命中的季集片段从标题区删除。
7. **严格按以下顺序清洗标题区**：
   a. 删除开头中文媒体序号 `^\d{1,4}[.．、_]\s*(?=[一-鿿])`（`14.奇异博士1` → `奇异博士1`）。仅当分隔符后紧跟中文字符时剥离，故 `50.First.Dates`（拉丁）、`2012`（无分隔符）、`007：大破天幕杀机`（冒号）不受影响。
   b. 先删除末尾发布组 `-XXX`，使 `x264-GROUP` 先恢复成可识别的 `x264`。
   c. 处理方括号：`【】`/`[]` 内整体命中噪音词时连括号删除，否则只剥括号保留文本。
   d. 剥离中文季数标记 `全\d{1,3}季`（下载站命名如 `黑镜 Black Mirror[全7季]` → `黑镜 Black Mirror`）。
   e. 将 NOISE_WORDS 按长度降序逐个做大小写不敏感的边界替换；边界为字符串首尾或非字母、数字、中文字符，因此 `h.264`、`国语中字` 可作为整体删除，又不会误删正常片名子串。
   f. `.`、`_` 替换为空格，合并连续空白后 strip。中文与数字混排不额外切分（`流浪地球2` 保持原样）。
8. 结果标题为空串 → `title=None`（调用方仅在没有可跳过 NFO 时置 manual_needed）。
9. 函数不抛任何异常；内部异常捕获后返回全 None 并 `logger.debug`。

`parse_episode_name(name)`：同上，优先返回季集，标题可为 None。v1 仅交付并测试该纯函数能力，
扫描器不遍历分集、数据库不持久化 season/episode；这是未来分集 NFO 功能的预留能力。

### 5.3 已知边界（写入测试）

| 输入 | 输出 |
|---|---|
| `2012 (2009)` | title=`2012`, year=2009 |
| `1917.2019.1080p` | title=`1917`, year=2019（裸年份取最后一个：2019；标题区=`1917.`） |
| `流浪地球2 (2023)` | title=`流浪地球2`, year=2023 |
| `【高清】星际穿越.2014.国语中字` | title=`星际穿越`（方括号剥离后清洗）, year=2014 |
| `繁花.2023.S01E01.mkv` | title=`繁花`, year=2023, season=1, episode=1 |
| `国语中字.某电影.2020.WEB-DL` | title=`某电影`, year=2020 |
| `14.奇异博士1(2016).Doctor Strange 2016 UHD BluRay REMUX 2160p HEVC Atmos TrueHD 7.1-PTer` | title=`奇异博士1`, year=2016（开头序号剥离） |
| `50.First.Dates (2004)` | title=`50 First Dates`（拉丁标题前数字保留） |
| `007：大破天幕杀机 (2012)` | title=`007：大破天幕杀机`（冒号分隔不剥离） |
| 空串 / `.....` / 纯噪音 | title=None |

---

## 6. M4 NFO 生成器 —— 输出结构规格

公开接口（`ScrapedMeta` 统一从 `app.scrapers.base` 导入）：

```python
def write_movie_nfo(folder: Path, meta: ScrapedMeta) -> Path
def write_tvshow_nfo(folder: Path, meta: ScrapedMeta) -> Path
def nfo_exists(folder: Path, media_type: str) -> bool
```

### 6.1 movie.nfo 规范输出（字段顺序固定）

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>星际穿越</title>
  <originaltitle>Interstellar</originaltitle>
  <year>2014</year>
  <rating>8.7</rating>
  <plot>这里是简介文本</plot>
  <genre>科幻</genre>
  <genre>冒险</genre>
  <uniqueid type="tmdb" default="true">157336</uniqueid>
</movie>
```

规则：
- rating 保留 1 位小数（`f"{rating:.1f}"`）；None 时整个节点省略。同理 originaltitle/year/plot 为 None 时省略。
- `<uniqueid>` 必有（无 source_id 的数据不允许走到写 NFO 这步）。
- 编码 UTF-8、缩进 2 空格、结尾换行。lxml：`etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True, pretty_print=True)`。测试断言 XML 结构、字段顺序、声明和末尾换行，不断言声明使用单引号还是双引号。
- tvshow.nfo：根节点 `<tvshow>`，`<title>` 用剧名；有年份时输出 `<year>`，无年份则省略，其他字段同 movie。
- 写入路径：`{folder}/movie.nfo` 或 `{folder}/tvshow.nfo`；先写 `{target.name}.tmp`（如 `movie.nfo.tmp`），再 `os.replace(tmp, target)`。写入或替换失败时在 `finally` 清理临时文件。

### 6.2 `nfo_exists(folder, media_type)`

`media_type=="movie"` → 检查 `movie.nfo`；`"tv"` → `tvshow.nfo`。只判存在性，不校验内容。

---

## 7. M5 TMDB 刮削器 —— 接口契约与字段映射

`app/scrapers/base.py` 是共享类型的唯一归属，M4/M5/M7 均从这里导入，不得在 tmdb.py 重复定义：

```python
@dataclass
class ScrapedMeta:                 # 可变，豆瓣补充阶段会替换 overview/rating
    source: str
    source_id: str
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    rating: float | None
    genres: list[str]
    poster_url: str | None
    backdrop_url: str | None

class TmdbScraper:
    def __init__(self, api_key: str, language: str = "zh-CN"): ...
    async def search_and_fetch(self, title: str, year: int | None,
                               media_type: str) -> ScrapedMeta | None: ...
    async def download_image(self, url: str, dest: Path) -> None: ...
    async def aclose(self) -> None: ...
```

### 7.1 请求规格

- Base URL：`https://api.themoviedb.org/3`；鉴权用 query 参数 `api_key=<v3 key>`。
- 公共参数：`language=zh-CN`（来自配置）。
- httpx.AsyncClient 复用单实例，`timeout=15.0`。
- `TmdbScraper.aclose()` 负责关闭客户端；应用关闭或设置重建 scraper 时必须调用。

| 步骤 | 端点 | 参数 |
|---|---|---|
| 搜电影 | `GET /search/movie` | `query`, `year`(可选), `language`, `api_key` |
| 搜剧集 | `GET /search/tv` | `query`, `first_air_date_year`(可选), `language`, `api_key` |
| 电影详情 | `GET /movie/{id}` | `language`, `api_key` |
| 剧集详情 | `GET /tv/{id}` | `language`, `api_key` |

### 7.2 响应字段 → ScrapedMeta 映射表

| ScrapedMeta 字段 | 电影来源字段 | 剧集来源字段 | 处理 |
|---|---|---|---|
| source | 常量 `"tmdb"` | 同 | |
| source_id | detail `id` | 同 | str() |
| imdb_id | detail `imdb_id` | 同 | 字幕精确匹配与 NFO uniqueid 用 |
| title | detail `title` | detail `name` | 空则用 original_* |

> imdb_id 缺失时（TMDB 剧集详情常缺），补调 `/movie|tv/{id}/external_ids` 取回。
| original_title | detail `original_title` | detail `original_name` | |
| year | `release_date` 前 4 位 | `first_air_date` 前 4 位 | 空串/None → None |
| overview | detail `overview` | 同 | 空串 → None |
| rating | detail `vote_average` | 同 | round(x,1)；0 → None |
| genres | detail `genres[].name` | 同 | list[str] |
| poster_url | `https://image.tmdb.org/t/p/original` + `poster_path` | 同 | poster_path 为 None → None |
| backdrop_url | 同上 + `backdrop_path` | 同 | 同 |

### 7.3 错误处理矩阵

| 响应 | 行为 |
|---|---|
| 200，results 为空 | 按候选查询×年份依次重搜，全部为空 → 返回 None |
| 200，results 非空 | 取 `results[0]` 的 id 进详情 |

搜索候选查询（由最精确到最宽松）：先原标题，再剥离末尾中文序号后的标题
（`奇异博士1` → `奇异博士`，因 TMDB 标题不含「1」）；每个查询先带年份再不带年份。
剥离规则：仅当数字紧跟 CJK 字符后，如 `^(.*[一-鿿])(\d+)$`，英文标题与 `猎杀T34` 不受影响。
| API Key 为空 | 发请求前抛 `TmdbAuthError("TMDB API Key 未配置")` |
| 401 | 抛 `TmdbAuthError("TMDB API Key 无效")`；scanner 先完成本地 NFO 跳过，再将尚未完成的 API 必需项置 failed |
| 429 | 读 `Retry-After` 秒数（缺省 2）→ sleep；首次请求外最多重试 3 次（总尝试 4 次）仍为 429 则抛 `TmdbRateLimitError` |
| 其他 4xx/5xx | 抛 `TmdbError(f"TMDB HTTP {code}: {path}")`，只记录端点路径，不含 query |
| 超时/连接错误 | 抛不含 `api_key` 的 `TmdbError("TMDB 网络错误: <类型与端点>")` |

`download_image(url, dest)`：`GET` 流式下载 → 写 `dest.with_name(dest.name + ".tmp")` →
`os.replace(tmp, dest)`；非 200、流中断、磁盘或替换错误均抛 `TmdbError`，并在 `finally` 删除残留临时文件。
任何异常和日志都必须移除 URL 中的 `api_key` 参数。

---

## 8. M6 豆瓣刮削器 —— 抓取规格

```python
@dataclass(frozen=True)
class DoubanSupplement:
    overview: str | None
    rating: float | None

class DoubanScraper:
    def __init__(self, delay_seconds: float): ...
    async def fetch_supplement(self, title: str,
                               expected_year: int | None) -> DoubanSupplement | None: ...
    async def aclose(self) -> None: ...
```

### 8.1 两步抓取

1. **建议接口（JSON，稳定性相对高）**
   `GET https://movie.douban.com/j/subject_suggest`，标题必须通过 httpx `params={"q": title}` 编码，
   响应先 `raise_for_status()` 再解析 JSON。
   响应数组项字段：`id`, `title`, `year`(str), `sub_title`, `img`, `url`。
   请求头必带：`User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...`（常量 UA），`Referer: https://movie.douban.com/`。
2. **年份校验**：候选 year 字符串先安全转换为 int；expected_year 与候选 year 都非空且不相等时
   依次看下一候选（最多前 3 条），全不符返回 None。任一方年份为空则允许该候选。
3. **详情页解析** `GET https://movie.douban.com/subject/{id}/`：响应先 `raise_for_status()`，再解析：
   - 评分：XPath `//strong[contains(concat(' ', normalize-space(@class), ' '), ' rating_num ')]` 文本 → float；无 → None。
   - 简介：XPath `//span[@property='v:summary']` 文本，`strip()` 并把连续内部空白压成单空格；无 → None。
   - XPath 以模块顶部常量 `XPATH_RATING` / `XPATH_SUMMARY` 维护，不依赖额外的 `cssselect` 包。

### 8.2 限流器（精确算法）

```python
class RateLimiter:
    def __init__(self, min_interval: float):
        if not math.isfinite(min_interval) or min_interval < 0.5:
            raise ValueError("delay must be finite and >= 0.5")
        self._min = min_interval
        self._last = 0.0            # monotonic
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            delta = self._last + self._min - now
            if delta > 0:
                await asyncio.sleep(delta)
            self._last = time.monotonic()
```

每次 HTTP 请求（含建议接口与详情页，**每个请求都计数**）前 `await limiter.wait()`。
wait 返回后以 DEBUG 记录 `豆瓣请求 <endpoint> monotonic=<值>`（不记录 query），供真实冒烟核对间隔。

### 8.3 异常策略

`fetch_supplement` 整体包 try/except：任何异常（网络、JSON 解析、HTML 结构变化、编码）→ `logger.warning("豆瓣抓取失败(%s): %s", title, exc)` → 返回 None。**该函数永不抛异常**。返回的 DoubanSupplement 中 overview/rating 均为 None 时等价返回 None。

`DoubanScraper.aclose()` 关闭其复用的 `httpx.AsyncClient`；应用关闭或设置重建时必须调用。

---

## 9. M7 扫描与刮削主流程 —— 状态机与流程伪代码

扫描器在关闭 Session 后携带不可变 DTO 进入网络阶段：

```python
@dataclass(frozen=True)
class ScrapeTarget:
    id: int
    folder_path: Path
    media_type: str
    parsed_title: str | None
    parsed_year: int | None
    status: str

@dataclass(frozen=True)
class ExistingNfoMatched:
    pass

ScrapeResult = ScrapedMeta | ExistingNfoMatched
```

持久化 `ExistingNfoMatched` 时只把 status 设为 matched 并清空 error_message，不伪造 source/标题等
元数据；持久化 `ScrapedMeta` 时按 §9.3 的字段白名单完整更新。

### 9.1 状态机转移表（唯一权威定义）

| 当前状态 | 事件 | 新状态 | 备注 |
|---|---|---|---|
| （无记录） | 发现 NFO 且 overwrite=false | pending | 进入队列后按 NFO 跳过并计入本轮 matched；标题可为空 |
| （无记录） | 无可跳过 NFO，解析成功 | pending | 写 parsed_title/parsed_year |
| （无记录） | 无可跳过 NFO，解析失败 | manual_needed | parsed_title=None |
| pending | 重新发现 | pending/manual_needed | 有可跳过 NFO时保持 pending；否则按最新解析结果决定 |
| failed | 重新发现且标题有效 | failed | 即使旧 NFO 仍在也必须重试，防止强制重刮失败被掩盖 |
| failed | 重新发现但标题仍为空 | manual_needed | 避免手动重刮无标题项后形成永久自动失败循环 |
| pending/failed/matched | 刮削成功 | matched | overwrite=true 时 matched 也可进入队列；成功后填元数据并清空错误 |
| pending/failed/matched | 刮削或必需图片下载失败 | failed | 写 error_message；单条失败不影响下一条 |
| 任意（非 missing） | 在已完整枚举的库中未发现 | missing | 记录保留；不可访问的库不得执行该转移 |
| missing/manual_needed | 重新发现且 NFO 存在、overwrite=false | pending | 进入队列后统一按 NFO 跳过 |
| missing/manual_needed | 无可跳过 NFO且解析成功 | pending | 重新写 parsed 字段 |
| missing/manual_needed | 无可跳过 NFO且解析失败 | manual_needed | parsed_title=None |
| matched | NFO 被删除、overwrite=false | pending/manual_needed | 按最新解析结果重新生成，不允许永久跳过 |
| matched | NFO 仍存在、overwrite=false | matched | 保持并跳过，不计入队列 |
| manual_needed | 文件夹改名后解析成功 | —— | 改名=新 folder_path=新记录 pending；旧记录变 missing |
| matched | 全量刮削筛选 | 跳过或入队 | overwrite=false 跳过；overwrite=true 入队强制重刮 |
| manual_needed/missing | 全量刮削筛选 | （跳过） | 不进入队列 |
| 任意 | 手动 rescrape | matched/failed | 强制执行，无视 NFO 存在 |

### 9.2 互斥入口与 `run_full()`

服务固定为单进程、单事件循环。`ScanRunner` 用同步设置的进程内 `_running` 预留标志实现互斥，
不得用 `asyncio.Lock.locked()` 后再异步 acquire 的方式拼接非原子检查。公开接口为：

```python
class ScanRunner:
    def __init__(self, session_factory, config: AppConfig,
                 tmdb: TmdbScraper, douban: DoubanScraper | None): ...
    async def run_full(self) -> ScrapeLog: ...
    def start_full_background(self) -> asyncio.Task[ScrapeLog]: ...
    def stop(self) -> bool: ...
    async def rescrape_item(self, item_id: int) -> MediaItem: ...
    async def _scrape_one(self, target: ScrapeTarget, *, force: bool) -> ScrapeResult: ...
    def reconfigure(self, config: AppConfig, tmdb: TmdbScraper,
                    douban: DoubanScraper | None) -> tuple[TmdbScraper, DoubanScraper | None]: ...
    @property
    def is_running(self) -> bool: ...
    async def shutdown(self) -> None: ...
```

`_claim()` 在任何 `await` 之前同步检查 `_accepting` 并设置 `_running=True`，已占用则抛
`ScanBusyError`；claim 失败不能调用 release。所有成功 claim 的入口都把当前 Task 登记到
`_current_task`，并仅在该 Task 的 `finally` 中清除和 `_release()`。`start_full_background()` 先 claim，
再创建并保存 Task；若 create_task 失败立即 release，done callback 必须消费并记录异常。
`run_full()` 和 `rescrape_item()` 也登记 `asyncio.current_task()`，因此 shutdown 能追踪调度、后台和
请求三种入口。

主流程伪代码如下；所有数据库事务必须在外部网络 `await` 前结束：

```
log_id = insert ScrapeLog(started_at=utcnow)                    # 短事务
total = matched = failed = 0; detail = []; queue_ids = []
try:
    libraries = load Library DTOs                               # Session 随即关闭
    fully_scanned_library_ids = set()
    found_paths_by_library = {}
    for lib in libraries:
        try:
            found = discover_items_recursive(lib)                # 见下方「目录发现」说明
        except OSError as exc:
            detail.append(f"库扫描失败: {lib.path}: {exc}")
            continue                                            # 不标记该库 missing
        normalized_found = {normalize_path(sub) for sub in found}
        with self._session_factory.begin() as session:
            for folder_path in normalized_found:
                upsert_item(session, lib, folder_path)          # 按 §9.1 恢复
        fully_scanned_library_ids.add(lib.id)
        found_paths_by_library[lib.id] = normalized_found       # dict[int, set[str]]

    with self._session_factory.begin() as session:
        mark_missing(session, fully_scanned_library_ids,
                     found_paths_by_library)                    # 只处理完整枚举的库
        statuses = ["pending", "failed"]
        if self._config.overwrite_existing_nfo: statuses.append("matched")
        queue_ids = select MediaItem.id where
                    MediaItem.library_id.in_(fully_scanned_library_ids) and
                    MediaItem.status.in_(statuses) order by MediaItem.id

    total = len(queue_ids)                                      # 唯一统计口径
    api_queue_ids = []
    for item_id in queue_ids:                                  # 先完成所有纯本地 NFO 跳过
        target = load immutable ScrapeTarget DTO                # Session 随即关闭
        if not self._config.overwrite_existing_nfo and target.status == "pending" \
           and nfo_exists(target.folder_path, target.media_type):
            persist ExistingNfoMatched in short transaction
            matched += 1
        else:
            api_queue_ids.append(item_id)

    for index, item_id in enumerate(api_queue_ids):
        target = load immutable ScrapeTarget DTO
        try:
            result = await self._scrape_one(target, force=False) # await 时无 DB 写事务
            persist matched/result in one short transaction
            matched += 1
        except TmdbAuthError as exc:
            remaining_ids = api_queue_ids[index:]
            bulk set remaining_ids status=failed/error_message # 一次短事务
            failed += len(remaining_ids)
            detail.extend(f"{path}: {exc}" for each remaining item)
            break                                               # 不再发注定失败的请求
        except asyncio.CancelledError:
            remaining_ids = api_queue_ids[index:]
            bulk set remaining_ids status=failed/error_message="任务因应用关闭而取消"
            failed += len(remaining_ids)
            detail.extend cancellation lines
            raise
        except Exception as exc:
            set item status=failed/error_message in short transaction
            detail.append(f"{target.folder_path}: {exc}")
            failed += 1
finally:
    update log_id: finished_at, total, matched, failed,
                   detail="\n".join(detail)                     # 短事务
return reload log_id with expire_on_commit=False
```

`fully_scanned_library_ids` 为空时队列必须为空。扫描阶段数据库异常也必须进入 finally，此时预先
初始化的 total/matched/failed 均为 0，保证 ScrapeLog 可以正常收尾。

目录发现采用**递归**而非只扫库根第一层（避免分组/深层结构漏检）：

- **电影**：一个目录「直接含 ≥1 个视频文件」即为一个电影条目并停止下钻（防止 Extras/Trailer 子目录被单独计数）；无直接视频时递归子目录。特例：
  - 库根目录直接放的散视频文件 → 每个散文件是一个**文件条目**（`folder_path` 指向视频文件本身）。
  - 目录含 `BDMV/`/`VIDEO_TS/` 子目录（光盘结构）→ 该目录即电影条目。
  - 无直接视频、且**恰有一个**子目录含视频、且自身名字解析出「标题+年份」→ 该目录为深层电影条目（如 `电影名 (2020)/Video/电影.mkv`）。
- **电视剧**：非根目录「直接含视频 **或** 含 Season 子目录（`Season 01`/`S01`/`第1季`）**或** 名字解析出标题且子目录为集数文件夹（`第3集`/`S01E02`，如 `黑镜 Black Mirror[全7季]/第3集（2016）/`）」即一部剧，命中即停止递归（避免每季/每集被多计）；否则递归。
- 递归时跳过噪音目录集合（`extras`/`trailers`/`sample(s)`/`screenshots`/`featurettes`/`behind the scenes`/`deleted scenes`/`interviews`/`making of`/`outtakes`/`bonus`/`extra`/`.actors`/`.backdrops`/`logo` 等）。
- `contains_video(folder)`：递归深度 ≤2 找 VIDEO_EXTENSIONS 文件（供深层判断与字幕定位使用，电影可能有 `BDMV/` 子层）。

**文件条目（散文件）**：`MediaItem.folder_path` 存视频文件完整路径，天然唯一；标题由 `parse_folder_name(文件名)` 剥离扩展名得到。NFO/图片命名按 Kodi 约定——文件条目写 `<视频文件名stem>.nfo` 与 `<stem>-poster.jpg`/`<stem>-fanart.jpg`（与视频同目录）；文件夹条目保持 `movie.nfo`/`tvshow.nfo` 与 `poster.jpg`/`fanart.jpg`。`ScrapeTarget.is_file` 由 `folder_path` 后缀推导。

`normalize_path(path)`：媒体库新增、seed 导入、扫描 upsert 全部共用。路径必须是绝对路径；执行
`os.path.normpath` 去除尾斜杠和 `.` 段后存为 POSIX 字符串。为允许尚未挂载的路径，不调用
`Path.resolve(strict=True)`；v1 不解析 symlink。相对路径由 Web 层拒绝，文案为 `路径必须是绝对路径`。

`ScrapeLog.total` 固定表示本轮进入自动处理队列的数量；`matched/failed` 表示该队列的最终结果。
原本已经 matched 且未启用覆盖、manual_needed、missing 均不计入 total。已有 NFO 的 pending/failed
条目会进入队列；pending 可按 NFO 跳过并计为 matched，failed 必须重试。无待处理项时三者均为 0。

`shutdown()` 先设 `_accepting=False`，给 `_current_task` 最多 30 秒完成；超时后 cancel 并 await 到
Task 真正结束。全量和单条 body 必须单独捕获 `asyncio.CancelledError`，把尚未完成项记为 failed、
完成 ScrapeLog 后再重新抛出。Runner 拥有当前 scraper；任务结束后由 shutdown 关闭当前 client。

`reconfigure()` 仅允许在 `is_running=False` 时调用，是不执行 I/O、不得抛异常的同步引用交换；
Runner 接管新 config/scraper 所有权并返回旧 scraper。提交后关闭旧 client 失败只记 warning，不回滚
到可能已经部分关闭的对象。应用最终关闭也只通过 Runner 关闭当前实例。

### 9.3 `ScanRunner._scrape_one(target)` 步骤

```
1. if not force and not self._config.overwrite_existing_nfo and target.status == "pending" \
      and nfo_exists(target.folder_path, target.media_type):
       return ExistingNfoMatched()                          # failed 永不被旧 NFO 自动转成功
2. if not target.parsed_title: raise ScrapeError("标题解析为空，无法搜索")
3. meta = await self._tmdb.search_and_fetch(target.parsed_title, target.parsed_year,
                                             target.media_type)
   if meta is None: raise ScrapeError("TMDB 无搜索结果")
4. if self._config.use_douban and self._douban:
       try: supp = await self._douban.fetch_supplement(target.parsed_title, meta.year)
       except Exception: logger.warning(...); supp = None   # 模块边界再隔离一次
       if supp:
           if supp.overview is not None: meta.overview = supp.overview
           if supp.rating is not None:   meta.rating = supp.rating
5. if meta.poster_url:   await self._tmdb.download_image(meta.poster_url, target.folder_path/"poster.jpg")
   if meta.backdrop_url: await self._tmdb.download_image(meta.backdrop_url, target.folder_path/"fanart.jpg")
6. write_{movie|tvshow}_nfo(target.folder_path, meta)        # 按 target.media_type 分派，完成标记最后写
7. return meta；调用方显式持久化 source/source_id/matched_title/
   matched_original_title/matched_year/overview/rating/poster_url/backdrop_url/
   genres=",".join(meta.genres)，并设 status="matched"、last_scraped_at、error_message=None
```

海报或背景图 URL 为 None 表示上游没有该资源，不算失败；只要 URL 存在，下载失败即按条目失败处理。
NFO 最后原子写入，因此失败不会产生或替换 NFO；强制重刮时旧 NFO 可保留，但 failed 状态会忽略
该旧文件并在下轮继续重试。

### 9.4 `rescrape_item(item_id)`

- 通过同一个 `_claim()` 占位，与全量和定时任务互斥。
- item 不存在 → 抛 `ItemNotFoundError`（Web 层转 404）。
- 调用 `self._scrape_one(target, force=True)`，跳过 §9.3 第 1 步。条目级异常在方法内转换为 failed，
  仅 busy/not-found 向 Web 抛出；`finally` 必须完成一条 `ScrapeLog(total=1, ...)`，detail 注明
  `手动重刮: {folder_path}` 及失败原因，方法返回最终 MediaItem。
- 网络 await 期间同样不得持有数据库事务。
- busy/not-found 在创建 ScrapeLog 之前返回，不写任务日志；只有成功加载目标后才创建
  `ScrapeLog(total=1)`，因此 detail 中始终有可用的 folder_path。

### 9.5 `download_subtitle(item_id)`

- 通过同一个 `_claim()` 占位，与全量/重刮互斥。
- 字幕功能未启用（`subtitle_enabled=false` 或未配置下载器）→ 抛 `ScrapeError`。
- item 不存在 → 抛 `ItemNotFoundError`（Web 层转 404）；标题为空 → 抛 `ScrapeError`。
- 与自动刮削共用 `_download_subtitle_for_target(target, conn, *, title, year, imdb_id=None)`：
  - 文件夹条目：`media_folder=Path(folder_path)`，`video_filename` 为相对该文件夹的片段（避免路径前缀重复）。
  - 文件条目（`is_file`）：`media_folder=Path(folder_path).parent`，`video_filename=文件名`（否则本地写入崩溃）。
- 手动条目标题优先 `matched_original_title`（英文原名，字幕站命中率高）→ `matched_title` → `parsed_title`；年份优先 `matched_year`；`imdb_id` 从条目读取传给下载器做精确匹配。
- **写一条 `ScrapeLog(total=1)`**：命中 `detail=手动字幕: <路径>: <文件名>`，未命中 `detail=手动字幕: <路径>: 未找到可用字幕`，异常 `detail=手动字幕: <路径>: <异常>`；`/logs` 页可见。
- 返回下载到的字幕路径（无匹配返回 `None`），Web 层据此提示。

---

## 10. M8 调度器 —— 精确行为

```python
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler

class ScrapeScheduler:
    def __init__(self, runner: ScanRunner):
        self._runner = runner
        self._scheduler = AsyncIOScheduler(
            timezone=os.environ.get("TZ", "Asia/Shanghai")
        )
        self._started = False
        self._cron = None

    def start(self, cron: str):
        trigger = validate_cron(cron)              # 与配置保存共用唯一解析器
        self._cron = cron
        self._scheduler.add_job(self._job, trigger, id="full_scrape",
                                max_instances=1, coalesce=True, misfire_grace_time=3600)
        self._scheduler.start()
        self._started = True

    async def _job(self):
        try:
            await self._runner.run_full()
        except ScanBusyError:
            logger.warning("定时任务触发时已有任务在运行，跳过本轮")

    def reschedule(self, cron: str):
        trigger = validate_cron(cron)              # 先解析成功，失败不动原 job
        self._cron = cron
        job = self._scheduler.get_job("full_scrape") if self._started else None
        if job is not None: job.reschedule(trigger)

    def pause(self):
        if self._started: self._scheduler.pause()

    @property
    def next_run_time(self):
        job = self._scheduler.get_job("full_scrape") if self._started else None
        return job.next_run_time if job is not None else None

    async def shutdown(self):
        if not self._started: return
        stopped = asyncio.get_running_loop().create_future()
        def on_shutdown(event):
            if not stopped.done(): stopped.set_result(None)
        self._scheduler.add_listener(on_shutdown, EVENT_SCHEDULER_SHUTDOWN)
        try:
            self._scheduler.shutdown(wait=False)
            await stopped
        finally:
            self._scheduler.remove_listener(on_shutdown)
            self._started = False
```

- `max_instances=1, coalesce=True`：错过的多次触发合并为一次。
- `start_scheduler=False` 时不创建 job；`next_run_time` 返回 None，`reschedule` 仍校验并缓存 cron，
  `shutdown` 是安全 no-op，确保测试模式可访问仪表盘和设置页。
- 应用关闭顺序固定为：`scheduler.pause()` 停止新触发 → `await runner.shutdown()` 等待/取消并关闭
  Runner 当前 scraper → `await scheduler.shutdown()` 等到 APScheduler 发出 shutdown 事件 →
  `engine.dispose()`。不得先 shutdown scheduler 取消仍在运行的 job。

---

## 11. M9 Web 层 —— 页面与路由契约

### 11.1 通用约定

- 提示消息：无 session，用重定向 query 参数 `?ok=<urlencoded文本>` / `?err=<urlencoded文本>`；base.html 顶部读取并渲染为绿/红横幅。
- 表单校验和业务冲突使用 `RedirectResponse(status_code=303)`；资源不存在仍按具体路由返回 404 JSON。
- 表单参数以可选字符串接收后手工校验，禁止让 FastAPI 在路由执行前返回与契约不符的 422。
- 模板公共上下文：`request`、`now_local`；过滤器 `localtime`（UTC→本地）、`mask_key`。
- 新增只读接口 `GET /healthz` → `{"status":"ok"}`（Docker 健康检查用）。

### 11.2 路由逐条契约

**GET `/`（dashboard.html）** 上下文字段：
`library_count`, `counts`（dict：pending/matched/failed/manual_needed/missing 各计数）, `is_running`(bool), `next_run_time`(本地时间或 "未启用"), `last_log`(ScrapeLog|None)。页面含表单 `POST /run-scrape`（按钮在 is_running 时 disabled 并显示"任务运行中…"）。

**POST `/run-scrape`**：
```
try: runner.start_full_background()       # 同步占位后才创建并保存 Task
except ScanBusyError: redirect("/?err=任务正在运行中，请稍后")
redirect("/?ok=任务已启动")
```

**POST `/stop-scrape`**（停止）：`runner.stop()` 为同步方法——仅置 `_stop_requested=True` 并 `task.cancel()`，不等待，故不会与扫描任务死锁。空闲 → `?err=当前没有正在运行的任务`；运行中 → `?ok=已请求停止，剩余条目将标记为已取消`。任务被取消时，`_run_full_impl` 的 `CancelledError` 分支把剩余条目置 failed，消息区分来源：`_stop_requested` 为真 → `任务已手动停止`，否则（应用关闭走 `shutdown()` 取消）→ `任务因应用关闭而取消`。互斥由任务 done-callback 释放（后台任务因 callback 不在任务上下文，`_done` 里显式清 `_current_task`），停止后 `is_running=False` 即可再起新扫描。

**GET `/libraries`（libraries.html）**：表格列＝名称/路径/类型/条目数/删除按钮；底部新增表单。
**POST `/libraries/add`** 表单字段：`name`(必填), `path`(必填), `media_type`(select: movie|tv)。
校验失败文案：`名称不能为空` / `路径不能为空` / `路径必须是绝对路径` / `该路径已存在` /
`类型无效`。先调用 §9.2 的 `normalize_path` 再做唯一性判断。路径不存在于磁盘时**允许添加**但提示
`ok=已添加（注意：当前容器内看不到该路径）`。任务运行中增删媒体库均返回
`?err=任务正在运行中，暂不能修改媒体库`，避免扫描与级联删除竞争。
**POST `/libraries/{id}/delete`**：不存在 → 404 JSON `{"detail":"library not found"}`；成功 → `?ok=已删除媒体库及其条目记录（磁盘文件未动）`。

**GET `/items`（items.html）**：
- Query 参数 `status`（可选，五值之一，非法值忽略）。
- 表格列：ID / 识别标题(parsed_title, manual_needed 时红字显示原文件名/文件夹名) / 年份 / 类型 / 匹配标题 / 评分 / 状态徽标 / 失败原因(error_message, title 属性放全文) / 操作(重新刮削 + 字幕按钮)。
- 状态徽标颜色：pending 灰、matched 绿、failed 红、manual_needed 橙、missing 深灰。
- 顶部过滤 tab：全部 + 五状态，显示各自计数。

**POST `/items/{id}/rescrape`**：成功 `?ok=已完成重新刮削: <matched|failed>`；运行中 `?err=任务正在运行中`；404 同上格式。
> 实现说明：rescrape 单条目同步执行（await），单条耗时秒级可接受；避免再造一套后台任务状态查询。

**POST `/items/{id}/subtitle`**（手动字幕刮削）：成功且命中 `?ok=字幕已下载: <文件名>`；未命中 `?err=未找到可用的字幕`；字幕未启用 `?err=字幕功能未启用…`；运行中 `?err=任务正在运行中`；404 同上格式。
> 实现说明：与自动刮削的字幕步骤共用 `_download_subtitle_for_target`；单条目同步执行。

**POST `/items/{id}/delete`**（删除当前记录）：仅删 `MediaItem` 行，磁盘文件不动；同时把 `normalize_path(folder_path)` 记入 `AppMeta["ignored_paths"]`（换行分隔），下次扫描跳过该路径不再重新加入。运行中 `?err=任务正在运行中…`；id 不存在 → 404。

**POST `/items/clear-ignored`**（清空忽略列表）：置空 `ignored_paths`，已删除路径下次扫描重新出现。运行中 → 错误提示。

`/items` 页顶部在忽略列表非空时显示「已忽略 N 条 … 清空忽略列表」；每行操作列新增「删除」按钮（`onsubmit` 二次确认）。

**GET `/logs`（logs.html）**：ScrapeLog 倒序前 50 条；列＝开始/结束(本地时间)/耗时/total/matched/failed/detail（折叠展开）。

**GET `/settings`（settings.html）** 表单字段（name 属性固定）：
| name | 控件 | 回显 |
|---|---|---|
| `tmdb_api_key` | password input | placeholder=`已设置(****abcd)，留空表示不修改` 或 `未设置` |
| `clear_tmdb_api_key` | checkbox | 勾选后清空 YAML Key，恢复环境变量回退；优先于 password input |
| `use_douban` | checkbox | checked 状态 |
| `douban_delay_seconds` | number step=0.5 min=0.5 | 当前值 |
| `overwrite_existing_nfo` | checkbox | checked 状态 |
| `schedule_cron` | text | 当前值；旁注下次运行时间 |

**POST `/settings`** 处理顺序：
1. 获取应用级 settings lock；**在锁内重新检查** runner，运行中则拒绝且不落盘。
2. 用 `validate_cron` 预构造 trigger；失败 → `?err=Cron 表达式无效，其余修改未保存`。
3. delay 必须满足 `math.isfinite(value) and value >= 0.5`；类型错误、NaN、正负无穷均整单拒绝。
4. `clear_tmdb_api_key` 勾选 → 更新为 `""`；否则 password 为空表示不修改。
5. 保存 `old_config`，令 `old_cron = old_config.schedule_cron`，并构造只含 §3.3 六个允许键的
   `rollback_updates`（不得直接 asdict，以免带入 libraries_seed/_extra）。根据候选配置构造新 scraper。
   锁内不执行 await：
   `save_config` → `scheduler.reschedule` → 不会失败的 `runner.reconfigure`，以引用交换成功为提交点。
6. 提交点前失败：用 old_cron 恢复 scheduler、用 old_config 回写文件；Runner 尚未交换。释放 lock 后
   await 关闭未采用的候选 client。提交点后不再回滚，释放 lock 后 best-effort await 关闭返回的旧
   client，失败仅记 warning。
7. `?ok=设置已保存`。

### 11.3 应用装配（main.py lifespan 顺序）

提供 `create_app(data_dir: Path | None = None, start_scheduler: bool = True) -> FastAPI`。测试传入临时
目录并可禁用真实调度器，生产模块级 `app` 调用默认参数创建。

```
1. data_dir = 参数 or get_data_dir(); data_dir.mkdir(parents=True, exist_ok=True)
2. config_path = data_dir/"config.yaml"; db_path = data_dir/"tmm-lite.db"; config = load_config(config_path)
3. engine = init_db(db_path); session_factory = create_session_factory(engine)
4. 在同一个 session_factory.begin() 事务内执行首次导入:
   if AppMeta.libraries_seed_imported 不存在:
       if Library 表空: 规范化并导入合法 seed（逐条预检查冲突/非绝对路径并记 warning）
       写 AppMeta.libraries_seed_imported="1"（即使 seed 为空也写）；任意 DB 异常整体回滚且不写 marker
5. tmdb = TmdbScraper(config.effective_tmdb_api_key, config.language)
   douban = DoubanScraper(config.douban_delay_seconds) if config.use_douban else None
6. runner = ScanRunner(session_factory, config, tmdb, douban)
7. scheduler = ScrapeScheduler(runner); if start_scheduler: scheduler.start(config.schedule_cron)
8. yield → 按 §10 的关闭顺序清理 scheduler/runner/engine
```

lifespan 必须用覆盖**整个资源获取阶段和 yield** 的 `try/finally`，记录 engine、scraper、runner、
scheduler 各自是否已创建。yield 前任一步失败也按实际创建进度逆序清理：Runner 已创建则由它关闭
当前 scraper，否则直接关闭已创建 scraper；scheduler 仅在已启动时关闭；engine 始终 dispose。

`effective_tmdb_api_key` 为空时应用照常启动，仪表盘显示 `未配置 TMDB API Key，需要联网刮削的条目将失败，请到设置页填写`。run-scrape 仍可点：本地 NFO 可跳过项 matched，API 必需项 failed 并记录认证错误。

---

## 12. M10 部署 —— 文件全文

### 12.1 `Dockerfile`

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 12.2 `docker-compose.yml`

```yaml
services:
  tmm-lite:
    build: .
    container_name: tmm-lite
    ports:
      - "8000:8000"
    environment:
      - TZ=Asia/Shanghai
      # - TMDB_API_KEY=你的key   # 也可在 Web 设置页填写（设置页优先）
      # - PUID=1000              # 如实现了用户映射入口脚本再启用
    volumes:
      - ./data:/app/data                      # 配置 + 数据库
      - /path/to/movies:/media/movies         # ← 改成你的电影目录
      - /path/to/tvshows:/media/tvshows       # ← 改成你的剧集目录
    restart: unless-stopped
```

### 12.3 `.dockerignore`

```
data/
tests/
.git/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
```

> v1.0 以 root 运行，以降低 NAS 媒体目录写权限配置复杂度；compose 注释保留 PUID 扩展位，
> README 必须明确只限内网及 root 容器风险，非 root 列入 v1.1。服务只允许单容器副本、单个
> Uvicorn worker；进程内调度器和互斥标志不支持多 worker/多副本。

---

## 13. 测试实施规格

### 13.1 异常体系（`app/exceptions.py` 全量）

```python
class TmmError(Exception): ...
class ConfigError(TmmError): ...
class TmdbError(TmmError): ...
class TmdbAuthError(TmdbError): ...
class TmdbRateLimitError(TmdbError): ...
class ScrapeError(TmmError): ...          # 条目级失败（无结果/标题为空）
class ScanBusyError(TmmError): ...
class ItemNotFoundError(TmmError): ...
```

### 13.2 pyproject.toml 关键配置

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
check_untyped_defs = true
```

### 13.3 Mock 夹具文件内容要点（`tests/fixtures/`）

- `tmdb_search_movie.json`：`{"results":[{"id":157336,"title":"星际穿越","release_date":"2014-11-07"}]}`
- `tmdb_movie_detail.json`：含 title/original_title/release_date/overview(中文)/vote_average=8.7/genres=[{科幻},{冒险}]/poster_path="/p.jpg"/backdrop_path="/b.jpg"
- `tmdb_search_tv.json` / `tmdb_tv_detail.json`：繁花(2023) 对应结构（name/first_air_date）。
- `douban_suggest.json`：`[{"id":"1889243","title":"星际穿越","year":"2014","url":"..."}]`
- `douban_subject.html`：最小 HTML，含 `<strong class="ll rating_num">9.4</strong>` 与 `<span property="v:summary">豆瓣中文简介文本</span>`。

### 13.4 conftest.py 公共夹具清单

| fixture | 内容 |
|---|---|
| `tmp_data_dir` | tmp_path 下 data 目录，显式传给 `create_app(data_dir=...)`；另单测 `get_data_dir` 的环境变量行为 |
| `db_session` | tmp_path 文件 SQLite；如使用 `sqlite://` 则必须加 StaticPool + PRAGMA 外键 |
| `movie_library(tmp_path)` | 构造 `movies/星际穿越 (2014)/Interstellar.2014.1080p.mkv`（空文件）等 3 部电影 |
| `tv_library(tmp_path)` | 构造 `tvshows/繁花 (2023)/Season 01/繁花.S01E01.mkv` 等 2 部剧 |
| `tmdb_mock` | respx router：注册 §13.3 全套响应，图片 URL 返回固定字节 `b"JPEGDATA"` |
| `douban_mock` | respx router：suggest + subject 页 |
| `app_client` | `with TestClient(create_app(data_dir=tmp_data_dir, start_scheduler=False)) as client: yield client`，确保 lifespan 执行 |

### 13.5 用例执行基线

开发文档 M1-T* ~ M10-T*、E2E-T1~T4 全部实现为自动化用例（M10 除外，为 CI shell 脚本或手动清单）。另新增：
- **M7-T13** 同时覆盖正常产物齐全，以及图片失败时条目 failed；新条目无 NFO，强制重刮则旧 NFO 不变且下轮仍重试。
- 新增 **M7-T16**：库路径不存在 → 该库跳过、detail 记录、该库原条目不变、其他库正常处理。
- 新增 **M7-T17**：overwrite 从 false 改为 true 后，已有 matched 条目进入队列并覆盖 NFO。
- 新增 **M7-T18**：本地 NFO 项先 matched；TMDB 认证失败后当前及剩余 API 必需项 failed，不再发请求。
- 新增 **M9-T10**：`GET /healthz` 返回 200 `{"status":"ok"}`。
- 新增 **M9-T11**：未配置 API Key 时仪表盘显示警告横幅。
- 新增 **M9-T12**：任务运行中设置请求被拒绝，文件、scheduler、scraper 均不变；热更新本身由 M9-T6 覆盖。

---

## 14. 验收执行手册（按序执行，全过即可上线）

### 14.1 自动化关卡（CI）

```bash
python -m pip install -r requirements-dev.txt
ruff check app tests
mypy app
pytest --cov=app --cov-report=term-missing --cov-report=json --cov-fail-under=75
python scripts/check_coverage.py coverage.json \
  app/parsers/filename_parser.py=85 app/scanner.py=85
```

`scripts/check_coverage.py` 把 coverage JSON 的文件键和命令行路径都执行 `replace("\\", "/")` 后，
读取 `files[路径].summary.percent_covered`；路径不存在或任一模块低于门槛均输出明确错误并非零退出。
CI 固定使用 Python 3.12。这样总体 75% 和 M3/M7 各 85% 都是实际门禁，而非注释。

### 14.2 部署验收（本机/NAS 手动，约 15 分钟）

| 步骤 | 操作 | 通过标准 |
|---|---|---|
| 1 | `docker compose build` | 成功，`docker images` 显示 <300MB |
| 2 | 准备假媒体目录（2 个正确命名电影文件夹+占位 .mkv），并把 compose 左侧 `/path/to/movies` 改成该目录的绝对路径 | 容器内 `/media/movies` 可见两部电影 |
| 3 | `docker compose up -d`，10s 后访问 `http://<host>:8000/` | 仪表盘 200，无 Key 警告横幅可见 |
| 4 | 设置页填入真实 TMDB Key 保存 | 提示"设置已保存"，警告横幅消失 |
| 5 | 添加电影媒体库 `/media/movies` | 列表出现，条目数 0（未扫描） |
| 6 | 点"立即执行一次" | 提示任务已启动；items 页出现 2 条并转为 matched |
| 7 | 检查宿主机媒体目录 | 每个电影文件夹内有 movie.nfo/poster.jpg/fanart.jpg，NFO 中文正常 |
| 8 | `docker compose restart` 后刷新 | 库、条目、设置全部还在 |
| 9 | logs 页 | 有一条记录，统计数正确 |

### 14.3 真实环境冒烟（发布前一次）

1. **媒体服务器互通**：把步骤 7 的目录接入 Jellyfin/Kodi/Emby 任一实机 → 海报、中文简介、评分正确显示（截图留存）。
2. **豆瓣链路**：临时把 `app.scrapers.douban` logger 调为 DEBUG，开启豆瓣并 rescrape 一个条目 →
   简介变为豆瓣文案（或年份校验/失败降级），按 monotonic 日志确认每个 HTTP 请求间隔 ≥2s，随后恢复日志级别。
3. **定时任务**：cron 临时改为 2 分钟后触发 → 到点自动执行、ScrapeLog 落库、时间为本地时区。改回 `0 4 * * *`。
4. **异常路径**：新增一个尚未扫描的电影目录，把 Key 改错一位后 run-scrape → 新条目 failed 且
   error_message 明确提示 Key 问题（原 matched 条目按约定跳过）；改回后 rescrape 新条目恢复 matched。

### 14.4 上线 checklist

- [ ] 14.1 CI 全绿（附覆盖率报告）
- [ ] 14.2 九步全过
- [ ] 14.3 四项全过（留存截图/日志）
- [ ] README 四要素齐全（快速开始/Key 申请/命名约定/安全提示"勿暴露公网"）
- [ ] 三份文档与实现一致（季集范围、图片失败、认证失败、NFO 覆盖、root 单进程等裁决均已同步）
- [ ] 打 git tag `v1.0.0`，镜像 tag 同步

---

## 15. v1.0 明确不做（防止范围蔓延）

- 分集 NFO、手动匹配 UI、多源优先级、通知、媒体服务器 API 联动（设计说明书 §17）。
- Basic Auth（README 安全提示替代）。
- 非 root 容器运行（§12.3 裁决，v1.1 处理）。
- TMDB 中文空字段回退英文（保留扩展点，v1.1）。
- `/api/*` JSON 接口（现有路由已可平移改造）。

---

*本文档为 v1.0 实施基线。实现过程中的任何偏离必须在对应章节追加"裁决记录"并同步测试用例，不允许只改代码不改文档。*
