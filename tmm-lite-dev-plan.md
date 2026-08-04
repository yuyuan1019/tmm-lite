# TMM-Lite 开发文档（模块细分 + 验收测试要求）

> 本文档基于《TMM-Lite 设计说明书》（`tmm-lite-design-spec.md`）编写，面向实际开发（人工或 AI），
> 将系统拆分为可独立开发、独立验收的模块，规定每个模块的职责边界、对外接口、实现要点与验收测试要求。
> 设计说明书回答"做什么"，本文档回答"怎么做、按什么顺序做、做到什么程度算完成"。
> 精确接口与冲突裁决以 `tmm-lite-implementation-spec.md` 为最终基线；优先级为实施规格书 > 本文档 > 设计说明书。

技术栈（与设计说明书第 3 节一致）：Python 3.12 + FastAPI + SQLAlchemy(SQLite) + APScheduler + httpx + Jinja2，Docker 部署。

测试框架约定：`pytest` + `pytest-asyncio`；HTTP 打桩用 `respx`（httpx 专用 mock）；Web 层测试用 `fastapi.testclient` / `httpx.AsyncClient`。
所有单元测试不得访问真实外网（TMDB/豆瓣一律 mock），集成验收阶段另有联网冒烟用例。

---

## 0. 模块总览与依赖关系

```
M1 config      ──┐
M2 database    ──┤（基础层，无相互依赖，可并行开发）
                 │
共享 DTO base.py   ──┐（先固定 ScrapedMeta 契约）
M3 filename_parser ──┤
M4 nfo_writer      ──┤
M5 scraper_tmdb    ──┤
M6 scraper_douban  ──┘
                 │
M7 scanner（扫描+刮削主流程）   依赖 M1–M6
M8 scheduler（定时任务）        依赖 M7
M9 web（路由+页面）             依赖 M1、M2、M7、M8
M10 deploy（Docker 打包）       依赖全部
```

| 模块编号 | 模块名 | 对应源文件 | 依赖 |
|---|---|---|---|
| M1 | 配置管理 | `app/config.py` | — |
| M2 | 数据层 | `app/database.py` | — |
| M3 | 文件名解析器 | `app/parsers/filename_parser.py` | — |
| M4 | NFO 生成器 | `app/nfo_writer.py` | 共享 `scrapers/base.py` DTO |
| M5 | TMDB 刮削器 | `app/scrapers/tmdb.py` | M1、共享 DTO |
| M6 | 豆瓣刮削器 | `app/scrapers/douban.py` | M1 |
| M7 | 扫描与刮削主流程 | `app/scanner.py` | M1–M6 |
| M8 | 调度器 | `app/scheduler.py` | M1、M7 |
| M9 | Web 层 | `app/main.py` + `templates/` + `static/` | M1、M2、M7、M8 |
| M10 | 部署 | `Dockerfile`、`docker-compose.yml` | 全部 |

开发顺序建议（里程碑见第 12 节）：先固定 `scrapers/base.py` DTO，再 M1/M2 → M3/M4 → M5/M6 → M7 → M8/M9 → M10。
共享 DTO 固定后，M3、M4、M5、M6 可并行。

---

## M1 配置管理（`app/config.py`）

### 职责
- 读取/写入 `data/config.yaml`；Runner 持有唯一“当前配置”引用，热更新通过同步引用交换，不使用模块级可变单例。
- 实现设计说明书第 13 节的**单一事实来源**规则：
  - 设置项（API Key、豆瓣开关、间隔、cron、覆盖 NFO 开关、language）以 YAML 为唯一持久化载体。
  - `libraries` 段仅供首次启动导入（导入动作由 M7/M9 调用方执行，本模块只负责读出该段）。
  - TMDB API Key 解析优先级：YAML 非空值 > 环境变量 `TMDB_API_KEY` > 空字符串。

### 对外接口（建议签名）
```python
@dataclass
class AppConfig:
    tmdb_api_key: str            # YAML 原始值，可能为空
    use_douban: bool
    douban_delay_seconds: float
    overwrite_existing_nfo: bool
    language: str
    schedule_cron: str
    libraries_seed: list[LibrarySeed]   # 仅首次导入用

    @property
    def effective_tmdb_api_key(self) -> str: ...

def validate_cron(cron: str) -> CronTrigger
def load_config(path: Path | None = None) -> AppConfig
def save_config(updates: dict, path: Path | None = None) -> AppConfig
```

### 实现要点
- 文件不存在时自动生成带默认值的 config.yaml（默认值见设计说明书第 13 节）。
- `save_config` 必须原子写（写临时文件后 rename），避免写一半进程被杀导致配置损坏。
- YAML 中未知字段保留不丢弃（向前兼容）；缺失字段用默认值补齐。
- cron 保存、应用启动和调度器重排共用 `validate_cron`：先要求恰好 5 段，再用
  `CronTrigger.from_crontab` 解析。禁止使用接受范围不同的第二套校验器。
- YAML 使用 `safe_load/safe_dump` 并严格校验字段类型；语法或语义错误均不覆盖原文件。
- `path=None` 时在调用时读取 `TMM_DATA_DIR`，不得把环境变量路径冻结在模块导入时。

### 验收测试要求
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M1-T1 | 首次加载（文件不存在） | 自动创建文件，返回全默认值配置 |
| M1-T2 | API Key 优先级 | YAML 填了值→用 YAML；YAML 为空且设了环境变量→用环境变量；都空→空字符串 |
| M1-T3 | save_config 局部更新 | 只改 `use_douban`，其余字段（含 libraries 段、未知字段）原样保留 |
| M1-T4 | 原子写 | 写入过程中检查不存在半成品状态（用临时文件名断言或注入 rename 失败） |
| M1-T5 | 非法 cron 拒绝保存 | `"abc"`、4 段、6 段及 APScheduler 不支持的扩展语法均抛异常，文件不变 |
| M1-T6 | 缺失/多余字段容错 | 手工删掉一个字段、加一个未知字段后加载不报错，缺失字段取默认值 |
| M1-T7 | 类型错误保护 | YAML 根节点/字段类型错误，以及 delay 为 NaN/正负无穷时抛 ConfigError，原文件不变 |

---

## M2 数据层（`app/database.py`）

### 职责
- 定义 SQLAlchemy 模型：`Library`、`MediaItem`、`ScrapeLog`、内部键值表 `AppMeta`（字段严格对照设计说明书第 6 节）。
- 提供 engine/session 管理与建表入口。

### 关键约束（必须在模型层面体现）
- `Library.path` 唯一约束。
- `MediaItem.folder_path` 唯一约束（幂等性保障，设计说明书第 16 节）。
- `MediaItem.status` 枚举：`pending / matched / failed / manual_needed / missing`。
- `MediaItem` 同时保存 `matched_original_title`、`matched_year`；v1 的 `source` 固定为 tmdb。
- `MediaItem.library_id` 外键；**删除 Library 时其下 MediaItem 级联删除**（页面上删库后条目不残留；磁盘文件不动）。
- 时间字段统一 UTC 存储，展示层再转本地时区。

### 对外接口
```python
def init_db(db_path: Path) -> Engine        # 建表（存在则跳过）
def create_session_factory(engine: Engine) -> sessionmaker[Session]
```

### 验收测试要求
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M2-T1 | 建表幂等 | 连续调用 `init_db` 两次不报错 |
| M2-T2 | folder_path 唯一 | 插入重复 folder_path 抛 IntegrityError |
| M2-T3 | Library.path 唯一 | 同上 |
| M2-T4 | 级联删除 | 删 Library 后其 MediaItem 全部消失，其他库条目不受影响 |
| M2-T5 | status 枚举 | 五种合法值均可写入读出；非法值被拒绝（应用层校验或 DB CHECK 均可） |
| M2-T6 | 全字段读写往返 | MediaItem 所有字段（含中文标题、None 值字段）写入后读出一致 |
| M2-T7 | 首次导入标记 | AppMeta 可持久化 libraries_seed_imported，删除全部 Library 不会清除此标记 |

Session 工厂固定使用 `expire_on_commit=False`；调用方使用上下文管理器关闭 Session。SQLite 读出的
naive datetime 按 UTC 解释。跨线程内存库必须使用 StaticPool，测试也可直接使用临时文件数据库。

---

## M3 文件名解析器（`app/parsers/filename_parser.py`）

### 职责
- 纯函数模块：输入文件夹名/文件名字符串，输出解析结果，**不做任何 IO**。
- 实现设计说明书第 8 节规则：噪音清洗、年份、标题、季号、集号。

### 对外接口
```python
@dataclass
class ParsedName:
    title: str | None      # None 且无可跳过 NFO时，调用方标记 manual_needed
    year: int | None
    season: int | None
    episode: int | None

def parse_folder_name(name: str) -> ParsedName      # 电影/剧集文件夹名
def parse_episode_name(name: str) -> ParsedName     # 预留能力；v1 不扫描或持久化分集
```

### 实现要点
- 噪音词表以模块级常量维护（分辨率/编码/来源/发布组/中文标签，包含 `国语中字` 等组合词），大小写不敏感。
- 年份规则：**括号内年份优先；无括号取最后一个** `(19|20)\d{2}` 匹配（设计说明书第 8 节修订版）。
- 季集必须先从完整输入提取，再按选中的年份 Match 位置截取标题，防止漏掉年份后的 `S01E02`。
- 标题清洗顺序：截取标题区 → 删除季集片段 → 去噪音词 → `._` 替换为空格 → 合并空格 → strip；清洗后为空为 `title=None`。

### 验收测试要求（表驱动，最少覆盖以下输入）
| 编号 | 输入 | 期望 |
|---|---|---|
| M3-T1 | `星际穿越 (2014)` | title=星际穿越, year=2014 |
| M3-T2 | `Interstellar.2014.1080p.BluRay.x264-GROUP` | title=Interstellar, year=2014（噪音全清） |
| M3-T3 | `2012 (2009)` | title=2012, year=2009（片名是年份，括号优先） |
| M3-T4 | `1917 (2019)` | title=1917, year=2019 |
| M3-T5 | `The.Wandering.Earth.2019` | title=The Wandering Earth, year=2019（无括号取最后一个年份） |
| M3-T6 | `繁花.S01E01.mkv` | season=1, episode=1 |
| M3-T7 | `某剧 第03集.mp4` | episode=3 |
| M3-T8 | `某剧 第5话.mp4` | episode=5 |
| M3-T9 | `Season 02`（子文件夹名） | season=2 |
| M3-T10 | `1080p.x264-GROUP`（纯噪音） | title=None（解析失败） |
| M3-T11 | `流浪地球2 (2023)` | title=流浪地球2, year=2023（片名尾部数字不被误删） |
| M3-T12 | `国语中字.某电影.2020.WEB-DL` | title=某电影, year=2020（中文噪音标签清除） |
| M3-T13 | `繁花.2023.S01E01.mkv` | title=繁花, year=2023, season=1, episode=1 |

**通过标准**：以上全部通过，且解析函数对任意字符串输入不抛异常（用 hypothesis 或至少一组乱码/空串/超长串冒烟）。

---

## M4 NFO 生成器（`app/nfo_writer.py`）

### 职责
- 输入刮削结果数据对象，生成符合 Kodi 标准的 `movie.nfo` / `tvshow.nfo`（设计说明书第 10 节），写入指定目录。
- 提供"目录内是否已存在 NFO"的检测函数（供 M7 的跳过逻辑使用）。

### 对外接口
```python
def write_movie_nfo(folder: Path, meta: ScrapedMeta) -> Path
def write_tvshow_nfo(folder: Path, meta: ScrapedMeta) -> Path
def nfo_exists(folder: Path, media_type: str) -> bool
```

### 实现要点
- 用 `lxml.etree` 构建 DOM 后序列化，**禁止字符串拼接 XML**（中文、`&`、`<` 等必须正确转义）。
- 输出带 XML 声明：`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>`，UTF-8 编码。
- `<genre>` 多值展开为多个节点；`<uniqueid type="tmdb" default="true">` 必填。
- 空字段（如 rating 为 None）省略节点而非输出空标签。
- 写文件采用 `{target.name}.tmp` + `os.replace(tmp, target)` 原子替换，所有失败路径清理临时文件。
- 测试按 XML 结构、字段顺序、UTF-8 声明和末尾换行断言，不绑定 lxml 声明引号样式。

### 验收测试要求
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M4-T1 | 标准电影 NFO | 输出可被 XML 解析器重新解析，字段值与输入一致，根节点 `<movie>` |
| M4-T2 | 剧集 NFO | 根节点 `<tvshow>`，结构正确 |
| M4-T3 | 特殊字符转义 | 简介含 `<`, `&`, 引号、emoji、中文时输出仍为合法 XML 且往返值一致 |
| M4-T4 | 多 genre | 3 个类型 → 3 个 `<genre>` 节点 |
| M4-T5 | 空字段省略 | rating=None 时无 `<rating>` 节点 |
| M4-T6 | uniqueid | 存在 `type="tmdb" default="true"` 属性且文本为 ID |
| M4-T7 | nfo_exists | movie 目录有 `movie.nfo` → True；无 → False；tv 对应 `tvshow.nfo` |
| M4-T8 | 覆盖已有文件 | 目标已存在时正常覆盖，无残留临时文件 |

---

## M5 TMDB 刮削器（`app/scrapers/tmdb.py`）

### 职责
- 封装 TMDB 搜索 + 详情接口（设计说明书 9.1），输出统一的 `ScrapedMeta` 结构。
- 内置 429 退避重试（`Retry-After`；首次请求外最多重试 3 次，总尝试 4 次）。

### 对外接口
```python
from app.scrapers.base import ScrapedMeta  # 唯一定义位置；可变 dataclass

class TmdbScraper:
    def __init__(self, api_key: str, language: str = "zh-CN"): ...
    async def search_and_fetch(self, title: str, year: int | None,
                               media_type: str) -> ScrapedMeta | None
    async def download_image(self, url: str, dest: Path) -> None
    async def aclose(self) -> None
```

### 实现要点
- 搜索：电影 `/search/movie?query=&year=`，剧集 `/search/tv?query=&first_air_date_year=`，均带 `language`。
- 命中策略：取搜索结果第一条；无结果且带了 year → 去掉 year 重试一次；仍无 → 返回 None（调用方记 failed，error_message="TMDB 无搜索结果"）。
- 429 处理：首次请求外最多重试 3 次，总尝试 4 次；耗尽后抛 `TmdbRateLimitError`。
- Key 为空或响应 401 抛 `TmdbAuthError`，供 M7 执行批次级失败处理。
- 网络异常/5xx：抛带上下文的自定义异常（`TmdbError`），由 M7 捕获转为条目 failed。
- 图片下载：流式写入临时文件后 rename；非 200 抛异常。
- 异常、日志和 error_message 只包含端点路径，不得包含 query 中的 API Key。

### 验收测试要求（全部用 respx mock，不访问真实 API）
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M5-T1 | 电影搜索命中 → 详情 | 返回的 ScrapedMeta 各字段与 mock 响应一一对应，poster_url 拼接正确 |
| M5-T2 | 剧集搜索命中 | 使用 `/search/tv` 与 `first_air_date_year` 参数（断言请求参数） |
| M5-T3 | 无结果 + year 回退 | 第一次带 year 无结果、第二次不带 year 命中 → 返回结果；两次都无 → None |
| M5-T4 | 429 重试 | mock 前 3 次返回 429、第 4 次 200 → 成功；连续 4 次 429 → 抛 TmdbRateLimitError |
| M5-T5 | 5xx / 网络超时 | 抛 TmdbError；HTTP 错误含状态码和端点 path，网络错误含类型和端点，均不含 query/Key |
| M5-T6 | language 参数 | TMDB search/detail API 请求 query 含 `language=zh-CN`；图片请求不追加 |
| M5-T7 | 图片下载 | 内容落盘一致（比对字节）；404 时抛异常且无残留文件 |
| M5-T8 | 认证与脱敏 | 空 Key/401 抛 TmdbAuthError；异常文本和日志不含 API Key |

---

## M6 豆瓣刮削器（`app/scrapers/douban.py`）

### 职责
- 非官方接口抓取，仅补充中文简介与评分（设计说明书 9.2）。
- 内置请求间隔限流（`douban_delay_seconds`）与年份二次校验。

### 对外接口
```python
@dataclass
class DoubanSupplement:
    overview: str | None
    rating: float | None

class DoubanScraper:
    def __init__(self, delay_seconds: float): ...
    async def fetch_supplement(self, title: str,
                               expected_year: int | None) -> DoubanSupplement | None
    async def aclose(self) -> None
```

### 实现要点
- 限流：模块内维护上次请求时间戳，不足间隔时 `asyncio.sleep` 补足；限流逻辑对调用方透明。
- **年份校验**：统一转成 int 比较；候选不符时继续检查下一条，最多前三条，全不符才返回 None。
- 任何异常（超时、解析失败、结构变化）内部捕获后返回 None 并 `logger.warning`，**绝不向外抛**（设计说明书 16 节：豆瓣失败不影响主流程）。
- 页面解析选择器集中在模块顶部常量区，便于结构变化时快速修补。
- 使用 lxml XPath 常量解析，不引入额外 cssselect 依赖。
- 请求使用 httpx `params={"q": title}` 并先 `raise_for_status()`；字段覆盖用 `is not None`，不用 truthy 判断。

### 验收测试要求
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M6-T1 | 正常命中 | 返回简介与评分，值与 mock 页面一致 |
| M6-T2 | 年份候选筛选 | 第一候选 2020、第二候选 2014 → 选第二条；前三条均不符 → None |
| M6-T3 | 年份缺失容忍 | expected_year=None 或豆瓣无年份 → 不做校验，正常返回 |
| M6-T4 | 限流间隔 | 连续两次调用，第二次实际发出时间距第一次 ≥ delay_seconds（mock 时钟或缩短间隔实测） |
| M6-T5 | 异常吞噬 | mock 超时/500/HTML 结构不符，均返回 None 不抛异常，且产生 warning 日志 |
| M6-T6 | 无搜索结果 | 返回 None |

---

## M7 扫描与刮削主流程（`app/scanner.py`）

系统核心模块，实现设计说明书 7.1/7.2 的完整流程。

### 职责
1. 目录遍历：电影库（含视频文件的子文件夹=条目）/ 剧集库（每个子文件夹=一部剧）。
2. 增量同步 DB：新增、更新 parsed 字段；仅成功完整枚举的库可执行 missing 标记。
3. 已有 NFO 跳过逻辑：pending 可跳过；failed 必须重试，防止旧 NFO 掩盖强制重刮失败。
4. 刮削编排：TMDB 主源 → 豆瓣补充 → 下载图片 → 最后写 NFO → 更新状态。
5. 先完成本地 NFO 跳过项；普通单条异常隔离，TMDB 认证失败仅将剩余 API 必需项置 failed。
6. 同步任务占位；短事务逐条落库；结束写 `ScrapeLog`。

### 对外接口
```python
class ScanRunner:
    def __init__(self, session_factory, config: AppConfig,
                 tmdb: TmdbScraper, douban: DoubanScraper | None): ...

    async def run_full(self) -> ScrapeLog          # 全量：定时任务与"立即执行"共用
    def start_full_background(self) -> asyncio.Task[ScrapeLog]
    def start_rescrape_failed_background(self) -> asyncio.Task[ScrapeLog]  # 一键重刮失败项
    def stop(self) -> bool                         # 请求停止：置标志 + task.cancel()，同步不阻塞
    async def rescrape_item(self, item_id: int) -> MediaItem   # 单条目，强制覆盖 NFO
    async def download_subtitle(self, item_id: int) -> Path | None  # 单条目手动字幕
    def reconfigure(self, config: AppConfig, tmdb: TmdbScraper,
                    douban: DoubanScraper | None) -> tuple[...]
    async def shutdown(self) -> None
    @property
    def is_running(self) -> bool                    # 供仪表盘展示

# ScanBusyError 统一从 app.exceptions 导入
```

### 实现要点
- 视频文件扩展名白名单常量：`.mkv .mp4 .avi .ts .m2ts .mov .wmv .flv .rmvb .iso` 等。
- 单进程事件循环内用同步 `_claim()` 检查并设置 `_running`，所有入口在任何 await 前占位并在 finally 释放。
  `start_full_background` 必须先占位再创建和保存 Task，消除连续 POST 的竞态。
- 状态机转移规则（必须严格实现）：
  - 新发现有可跳过 NFO → pending 后入队计为 matched；否则解析成功 pending、失败 manual_needed
  - 仅完整枚举的库中未发现条目 → `missing`；不可访问的库保持原状态
  - missing/manual 重新发现有可跳过 NFO → pending；否则按解析结果 pending/manual_needed
  - failed 重新发现且标题有效时仍为 failed 并忽略旧 NFO；标题仍为空则回到 manual_needed
  - matched 的 NFO 被删除 → 按解析结果 pending/manual_needed；NFO 仍在且不开覆盖则保持 matched
  - 默认队列为 pending/failed；overwrite=true 时 matched 也入队
  - 图片 URL 存在时下载失败、NFO 写入失败或其他条目错误 → failed；NFO 最后写，避免失败后被误判完成
  - `manual_needed` / `missing` 不进入自动队列
- 豆瓣调用在 scraper 和 scanner 两层捕获异常；搜索参数使用 parsed_title，校验年份使用 TMDB 匹配年份。
- `rescrape_item`：无视已有 NFO 强制覆盖；条目为 `manual_needed` 时也允许手动触发（此时若解析标题为空则直接失败并给出明确 error_message）。
- `ScrapeLog.detail` 至少记录每个 failed 条目一行（folder_path + 原因）。
- `ScrapeLog.total` 是本轮进入队列的数量，matched/failed 是该队列最终结果；无待处理项时三者均为 0。
- 所有路径经统一 normalize 后以绝对 POSIX 字符串存储；外部 HTTP await 期间不得持有 SQLite 写事务。

### 验收测试要求（tmp_path 构造假目录树 + mock 刮削器）
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M7-T1 | 首次全量扫描 | 成功 mock 下 3 部电影 2 部剧均 matched，ScrapeLog=total 5/matched 5/failed 0 |
| M7-T2 | 幂等重扫 | 同一目录跑两次 → MediaItem 数量不变，无重复记录 |
| M7-T3 | matched 跳过 | 已 matched 条目重跑全量 → mock 刮削器断言未被调用 |
| M7-T4 | 已有 NFO 状态矩阵 | pending + NFO 可跳过为 matched；failed + NFO 仍重试；matched 丢失 NFO 后重新生成 |
| M7-T5 | 解析失败 → manual_needed | 纯噪音文件夹 → manual_needed，不进入刮削，ScrapeLog 不计为 failed |
| M7-T6 | 磁盘删除 → missing | 成功枚举的库中删掉一个文件夹再扫 → missing，记录保留 |
| M7-T7 | missing 恢复 | 恢复文件夹且有 NFO → pending 入队后计为 matched；否则按解析结果处理 |
| M7-T8 | 单条目失败隔离 | mock 让第 2 个条目抛异常 → 该条 failed 有 error_message，第 1/3 条正常 matched，任务不中断 |
| M7-T9 | 豆瓣失败不影响主流程 | douban 返回 None/抛错被吞 → 条目仍 matched，字段为 TMDB 值 |
| M7-T10 | 豆瓣覆盖生效 | douban 正常返回 → overview/rating 为豆瓣值，其余为 TMDB 值 |
| M7-T11 | 任务互斥 | run_full 运行中再调任一入口均抛 ScanBusyError；连续两次后台启动只有一次被接受 |
| M7-T12 | rescrape 强制覆盖 | matched + 已有 NFO 条目 rescrape → 刮削器被调用、NFO mtime 变化 |
| M7-T13 | 产物与图片失败 | 正常时产物齐全；图片失败时 failed，新条目无 NFO；强制重刮则旧 NFO 不变且下轮仍重试 |
| M7-T14 | ScrapeLog 统计 | 数字严格按“进入队列及最终结果”口径，detail 含失败条目路径；全跳过时 total=0 |
| M7-T15 | 电影库识别规则 | 无视频文件的子文件夹不建条目；剧集库子文件夹一律建条目 |
| M7-T16 | 库不可访问 | 根路径不存在/PermissionError 时该库原条目状态不变，detail 记录，其他库继续 |
| M7-T17 | 覆盖已匹配项 | matched 条目在 overwrite=false 时跳过，改 true 后进入队列并覆盖 NFO |
| M7-T18 | 认证批次失败 | 本地 NFO 项先 matched；首个 API 项认证失败后剩余 API 项均 failed，后续 TMDB 零调用 |
| M7-T19 | 短事务 | TMDB mock 挂起期间另一个 Session 可完成普通数据库写入，不出现 database is locked |
| M7-T20 | 关闭生命周期 | 三种入口均可被 shutdown 跟踪；超时取消后条目 failed、日志收尾、无未消费 Task 异常 |
| M7-T21 | 无标题重刮 | manual_needed 手动重刮失败后，下次扫描恢复 manual_needed，不进入永久自动失败循环 |

---

## M8 调度器（`app/scheduler.py`）

### 职责
- APScheduler（AsyncIOScheduler + CronTrigger）按 `schedule_cron` 触发 `ScanRunner.run_full`。
- 支持**热更新**：设置页保存新 cron 后调用 `reschedule(cron)` 生效，无需重启。

### 对外接口
```python
class ScrapeScheduler:
    def __init__(self, runner: ScanRunner): ...
    def start(self, cron: str) -> None
    def reschedule(self, cron: str) -> None
    def pause(self) -> None
    async def shutdown(self) -> None
    @property
    def next_run_time(self) -> datetime | None     # 供仪表盘展示
```

### 实现要点
- 定时触发时若锁被占（上一轮未跑完/手动任务在跑）→ 捕获 `ScanBusyError`，记 warning 日志跳过本轮，**不排队堆积**。
- `start/reschedule` 与 M1 共用 `validate_cron`，再把同一个 trigger 交给 job；非法 cron 时原任务保持不变。
- 未 start 的测试模式下，next_run_time=None，reschedule 只校验并缓存，pause/shutdown 安全 no-op。
- 应用关闭先 pause 新触发，再 await runner.shutdown，最后 await scheduler.shutdown 的关闭事件。
- 时区取容器本地时区（部署层保证 TZ 正确，见 M10）。

### 验收测试要求
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M8-T1 | 按 cron 触发 | 设 `* * * * *` 级别的近时触发（或直接触发 job 函数）→ runner.run_full 被调用 |
| M8-T2 | 热更新 | start 后 reschedule 新表达式 → next_run_time 按新表达式变化，旧 job 不残留（job 数量=1） |
| M8-T3 | 非法 cron | bad/4 段/6 段均抛异常，原 job 与 next_run_time 不变 |
| M8-T4 | 锁占用跳过 | runner 抛 ScanBusyError → 调度器不崩溃、日志有 warning、下次仍正常触发 |
| M8-T5 | shutdown 干净退出 | shutdown 后无遗留任务、事件循环可正常关闭 |
| M8-T6 | 禁用调度测试模式 | 未 start 时 next_run_time=None，reschedule/暂停/关闭均不报错 |

---

## M9 Web 层（`app/main.py` + `templates/` + `static/`）

### 职责
- 实现设计说明书第 11/12 节全部页面与路由。
- 应用启动装配：初始化 config → DB（含 libraries 首次导入）→ scrapers → runner → scheduler。

### 路由与行为规约（在设计说明书 12 节基础上明确响应行为）
| 方法+路径 | 行为 | 成功响应 | 失败响应 |
|---|---|---|---|
| GET `/` | 仪表盘 | 200 页面：库数、各状态计数、运行状态（is_running）、下次定时时间、最近一条 ScrapeLog | — |
| POST `/run-scrape` | 后台启动全量任务（不阻塞请求） | 303 重定向回 `/` 带成功 flash | 任务运行中 → 303 + "任务运行中"提示（不报 500） |
| GET `/libraries` | 库列表 + 各库条目数 | 200 | — |
| POST `/stop-scrape` | 请求停止当前扫描 | 303 回 `/` | 运行中 → 成功提示；空闲 → 错误提示 |
| POST `/rescrape-failed` | 一键重刮失败项（后台，限速） | 303 回 `/` | 无失败 → 提示；运行中 → 提示；否则开始 |
| POST `/libraries/add` | 新增库 | 303 回列表 | path 非绝对/重复/为空、类型非法或任务运行中 → 303 + 错误提示 |
| POST `/libraries/{id}/delete` | 删库（级联删条目，不动磁盘） | 303 | id 不存在 → 404 |
| GET `/items` | 条目表格，支持 `?status=` 过滤 | 200 | — |
| GET `/scan-live` | 实时刮削日志页 | 200 | 运行中自动刷新 |
| GET `/api/search` | 手动匹配候选搜索 | JSON | title 空 → 空列表；media_type 非法 → 400 |
| POST `/items/{id}/rescrape` | 单条目重刮 | 303 回 `/items` | 运行中 → 提示；id 不存在 → 404 |
| POST `/items/{id}/subtitle` | 单条目手动字幕 | 303 回 `/items` | 命中 → 成功提示；未命中/未启用/运行中 → 错误提示；id 不存在 → 404 |
| POST `/items/{id}/delete` | 删除记录（不删文件，路径进忽略列表） | 303 回 `/items` | 运行中 → 提示；id 不存在 → 404 |
| POST `/items/clear-ignored` | 清空忽略列表 | 303 回 `/items` | 运行中 → 提示 |
| GET `/logs` | ScrapeLog 倒序列表 | 200 | — |
| GET `/settings` | 设置表单（API Key 脱敏显示） | 200 | — |
| POST `/settings` | 保存设置 + 热更新运行对象 | 303 + 成功提示 | 非法 cron/任务运行中 → 303 + 错误提示且不落盘 |

### 实现要点
- 提供 `create_app(data_dir=None, start_scheduler=True)`；测试显式注入临时目录并关闭真实调度器。
- 首次导入由 `AppMeta.libraries_seed_imported` 判断；处理一次后即写标记，不能只检查 Library 是否为空。
- `/run-scrape` 只调用 `runner.start_full_background()`，由 Runner 在创建 Task 前同步占位并保存 Task 引用。
- 任务运行中拒绝媒体库增删和设置修改，避免级联删除、配置替换与扫描竞争。
- 设置流程进入应用级 lock 后重新检查 runner；无 await 地完成校验、落盘、scheduler 重排和同一个
  Runner 的同步引用交换。提交前失败恢复旧文件/cron并关闭候选 client；提交后关闭旧 client 失败只记 warning，不回滚。
- API Key 回显脱敏，空 password 表示“不修改”；另提供“清除已保存 Key”复选框恢复环境变量回退。
- 表单用可选字符串接收并手工校验，避免 FastAPI 默认 422 破坏 303 契约；显式资源不存在仍返回 404。
- 状态徽标五种状态五种颜色；failed 行展示 error_message。
- 页面为 Jinja2 服务端渲染，无前端构建链（设计说明书第 3 节）。

### 验收测试要求（TestClient，mock runner/scheduler）
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M9-T1 | 全页面 200 冒烟 | 5 个 GET 页面在空库/有数据两种状态下均 200，无模板渲染异常 |
| M9-T2 | 库增删 | 路径规范化后 add；相对/重复 path 拒绝；delete 级联；任务运行中增删均拒绝 |
| M9-T3 | run-scrape 触发与拒绝 | 空闲时 start_full_background 被调且立即 303；连续请求仅一次成功 |
| M9-T4 | rescrape | 有效 id → runner.rescrape_item(id) 被调用；无效 id → 404 |
| M9-T5 | items 过滤 | `?status=failed` 只展示 failed 条目 |
| M9-T6 | 设置保存与热更新 | 改 cron/key/豆瓣配置后同一 Runner 使用新对象且旧 client 关闭；非法 cron 文件和运行态都不变 |
| M9-T7 | API Key 脱敏与清除 | HTML 无完整 key；空输入不修改；勾选清除后 YAML 为空并回退环境变量 |
| M9-T8 | 首次导入 | seed 导入并写 AppMeta；删除全部库再次重启也不重复导入 |
| M9-T9 | 仪表盘数据 | 各状态计数、is_running、最近日志与 DB 实际一致 |
| M9-T10 | 健康检查 | GET /healthz 返回 200 `{"status":"ok"}` |
| M9-T11 | 缺少 Key | 仪表盘显示警告；本地 NFO 项 matched，API 必需项有明确认证错误 |
| M9-T12 | 任务中设置 | runner 运行时 POST settings 返回提示，文件、scheduler、scraper 均不变 |

---

## M10 部署（`Dockerfile` + `docker-compose.yml`）

### 职责
- 单容器镜像（`python:3.12-slim` 基础），`data/` 与媒体目录 volume 挂载，暴露 8000 端口。

### 实现要点
- compose 示例包含：`./data:/app/data`、`/your/movies:/media/movies`、`/your/tvshows:/media/tvshows`、`TZ=Asia/Shanghai`、`TMDB_API_KEY` 环境变量注释示例。
- v1 镜像以 root 运行以简化 NAS 写权限；README 明示风险，非 root/PUID 映射列入 v1.1。
- 固定单容器副本、单 Uvicorn worker；不得通过 `--workers` 或横向副本重复启动调度器。
- 健康检查固定打轻量 `GET /healthz`。
- `.dockerignore` 排除 data/、测试、.git。

### 验收测试要求（手动/CI 脚本）
| 编号 | 用例 | 通过标准 |
|---|---|---|
| M10-T1 | 构建 | `docker build` 成功，镜像 < 300MB |
| M10-T2 | 冷启动 | `docker compose up` 后 10s 内 `GET :8000/` 返回 200 |
| M10-T3 | 数据持久化 | 添加一个库 → `docker compose down && up` → 库仍在（config/db 落在 volume） |
| M10-T4 | 时区 | 容器内 `date` 与宿主时区一致；设置页显示的下次运行时间符合本地时间预期 |
| M10-T5 | 媒体卷写入 | 容器内可在挂载的媒体目录写测试文件（验证 nfo/图片可落盘） |
| M10-T6 | 单进程约束 | Docker CMD 无 `--workers`，README 明确只运行一个副本 |

---

## 11. 集成与端到端验收（全模块完成后执行）

### 11.1 自动化集成测试（mock 外网）
构造完整假媒体库（tmp 目录）+ respx 全套 TMDB/豆瓣 mock，走真实 FastAPI 应用（TestClient）：

| 编号 | 场景 | 通过标准 |
|---|---|---|
| E2E-T1 | 全链路快乐路径 | 添加库 → run-scrape → 轮询至任务结束 → items 页全部 matched，磁盘上 nfo/poster/fanart 齐全，NFO 可解析且字段正确，logs 页有记录 |
| E2E-T2 | 混合状态库 | 库中含：正常片、纯噪音文件夹、TMDB 无结果片、豆瓣年份不符片 → 状态分别为 matched / manual_needed / failed（有 error_message）/ matched（保留 TMDB 简介） |
| E2E-T3 | 二次运行幂等 | E2E-T1 后再 run-scrape → 外部 API 零调用，ScrapeLog.total/matched/failed 均为 0 |
| E2E-T4 | 设置变更全链路 | 使用 `start_scheduler=True` 的独立 app fixture，改豆瓣开关+cron 后新增 pending 条目 → 豆瓣调用符合新开关且 next_run_time 更新 |

### 11.2 真实环境冒烟（发布前手动执行一次）
1. 真实 TMDB Key + 2–3 部真实命名的电影空目录（放个小视频占位文件即可）。
2. Docker 部署跑一轮：确认真实 API 命中、中文元数据正确、Jellyfin/Kodi 任一实机导入该目录能识别海报与简介。
3. 豆瓣开启状态跑一轮，观察限流间隔日志正常、无封禁迹象。
4. 让容器跨过一次 cron 触发点，确认定时任务自动执行且 ScrapeLog 落库。

### 11.3 发布验收清单（Definition of Done）
- [ ] M1–M9 全部单元/集成测试通过，CI 绿色；核心模块（M3/M7）语句覆盖率 ≥ 85%，整体 ≥ 75%。
- [ ] E2E-T1 ~ T4 通过。
- [ ] 11.2 真实冒烟 4 项通过，附截图/日志记录。
- [ ] README 包含：compose 快速开始、TMDB Key 申请指引、媒体目录命名约定、"勿暴露公网"安全提示。
- [ ] 设计说明书与实现不一致处已回写更新文档。

---

## 12. 里程碑计划

| 里程碑 | 内容 | 验收门槛 |
|---|---|---|
| MS1 基础层 | M1 + M2 | M1/M2 全部用例通过 |
| MS2 纯函数层 | M3 + M4 | M3/M4 全部用例通过（可与 MS3 并行） |
| MS3 刮削器 | M5 + M6 | M5/M6 全部用例通过（mock） |
| MS4 核心流程 | M7 | M7 全部用例通过 —— **项目最大风险点，预留最多时间** |
| MS5 服务化 | M8 + M9 | M8/M9 用例 + E2E-T1~T4 通过 |
| MS6 交付 | M10 + 文档 | 11.2 冒烟 + 11.3 清单全勾 |

---

## 13. 通用开发约定

- **异常体系**：自定义异常统一放 `app/exceptions.py`（含 `TmdbAuthError`），各模块只导入，不重复定义。
- **日志**：标准 `logging`，模块级 logger（`logging.getLogger(__name__)`）；刮削主流程 INFO 级记录每条目结果，豆瓣异常 WARNING，堆栈仅在 DEBUG。
- **异步约定**：刮削/IO 链路全异步；阻塞操作（目录遍历可接受同步，量大时用 `asyncio.to_thread`）。
- **类型标注**：全部公开接口带类型注解，CI 跑 `ruff` + `mypy`（宽松模式起步）。
- **测试目录**：`tests/` 镜像 `app/` 结构；数据库使用 tmp 文件，若用内存 SQLite 必须配 StaticPool。
- **事务约定**：外部网络 await 期间不得持有 SQLite 写事务，扫描同步和逐条结果分别短事务提交。
- 磁盘路径统一 `pathlib.Path`，入库前统一规范化为 POSIX 风格绝对路径字符串。

---

*本文档与设计说明书、实施规格书配套使用；冲突以实施规格书为准，并同步回写其余文档与测试。*
