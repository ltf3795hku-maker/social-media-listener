from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# 这个文件只放“数据形状”，不做业务逻辑。
# 读项目时可以先看这里：它告诉你一次采集/分析会传入哪些配置，
# 以及采集结果最终会被整理成哪些统一字段。
# 注意 \bhku\b 匹配不到 "HKUBS"（后面紧跟字母没有词边界），所以 hkubs 必须单独列出。
HKU_SCOPE_PATTERN = r"(?i)(?:\bhku\b|\bhkubs\b|hku\.hk|香港大学|港大|university\s+of\s+hong\s+kong)"

# TikHub 搜索一页大约返回 20 条结果；页数 × 该值就是一次搜索的采集上限。
SEARCH_PAGE_SIZE = 20

# 报告窗口长度：从 run 起始时刻往前推的精确 8 × 24 小时，Broad / Topic / 竞对共用同一口径。
#
# 用 8 天而不是 7 天，是为了吸收「窗口锚在时刻、而非日期」带来的截断：
# 7 天窗口下，下午跑的 run 会把 7 天前上午发布的帖子切掉（实测 8/24 那批就有一条
# 只早了 7.6 小时被排除）。多留 24 小时可以覆盖一整天的运行时刻漂移。
#
# 刻意只保留一个常量：同一份报告体系里不应该并存两种「本周」的定义。
REPORTING_WINDOW_DAYS = 8

# 只有当搜索本身就限定「一周内」时才启用硬窗口过滤；
# 用户在高级选项里主动选择更长的发布时间范围时，不应被周报窗口截断。
WEEKLY_TIME_FILTER = "一周内"


@dataclass
class CollectConfig:
    """一次关键词采集任务的参数。"""

    # 【运行时必填】每次搜索的小红书关键词，例如“港大 Capstone”“港大 ba”。
    keyword: str
    # 【可调整】搜索翻页数量；页数越多，TikHub 调用越多。
    max_pages: int = 3
    # 【可调整】最多保留多少条笔记进入详情采集。
    max_notes: int = 20
    # 评论默认不采；如开启，则默认只抓互动达标的笔记，控制 TikHub 调用量。
    comment_pages: int = 0
    # 【可调整】每条一级评论下最多抓几页二级评论；通常先保持 0。
    sub_comment_pages: int = 0
    # 【可调整】只有点赞数达到阈值的笔记才抓评论；设为 0 表示所有笔记都抓。
    comment_min_likes: int = 500
    # 【可调整】只有评论数达到阈值的笔记才抓评论；设为 0 表示不按评论数过滤。
    comment_min_comments: int = 0
    # 评论采集范围：none=仅帖子；threshold=按阈值抓；top_notes=只抓高互动帖子评论；all=尽量抓所有入选帖子评论。
    comment_policy: str = "none"
    # top_notes 模式下按互动分取前 N% 帖子，不使用固定 likes/comments 阈值。
    comment_top_percent: int = 20
    # top_notes 模式下的安全上限，避免样本很大时评论请求失控；0 表示不上限。
    fetch_comments_for_top_notes: int = 0
    # 【前端选择项】排序：general=综合排序；time_descending=最新；popularity_descending=最热。
    sort_type: str = "general"
    # 【前端选择项】笔记类型：普通笔记 / 视频笔记 / 不限；当前默认只采图文普通笔记。
    note_type: str = "普通笔记"
    # 【前端选择项】发布时间：不限 / 一天内 / 一周内 / 半年内。
    time_filter: str = "不限"
    # 【可调整】默认只保留港大/HKU 范围内的帖子用于评论采集；设为 None 可关闭范围标签。
    scope_pattern: Optional[str] = HKU_SCOPE_PATTERN
    # 【可调整】指定输出目录；不填则写入 data/runs/<run_id>。
    output_dir: Optional[str] = None
    # 采集模式：topic_scan=单关键词主题监听；broad_scan=日常宽口径监听。
    scan_mode: str = "topic_scan"
    # 周报窗口长度（天）。只在 time_filter == WEEKLY_TIME_FILTER 时生效；0 表示不做硬窗口过滤。
    reporting_window_days: int = REPORTING_WINDOW_DAYS


@dataclass
class KeywordConfig:
    """Broad Scan 中一个关键词及其采集上限。

    页数是唯一的采集上限：TikHub 一页约 20 条，所以 max_notes 应当等于
    max_pages × SEARCH_PAGE_SIZE。若显式传入更小的 max_notes，多请求的那几页会被浪费。
    """

    keyword: str
    max_pages: int = 1
    max_notes: int = SEARCH_PAGE_SIZE

    @property
    def note_cap(self) -> int:
        """本关键词实际生效的笔记上限。"""

        return max(1, int(self.max_notes or 0)) if self.max_notes else self.max_pages * SEARCH_PAGE_SIZE


@dataclass
class CommentPolicy:
    """Broad Scan 评论策略；默认不抓评论，前端可展开 Edit Settings 修改。"""

    default: str = "none"  # none / top_notes / all
    fetch_comments_for_top_notes: int = 10
    top_percent: int = 20
    max_comments_per_note: int = 20
    min_comment_count: int = 0


@dataclass
class BroadScanConfig:
    """Social Monitoring Broad Scan 测试配置：总采集上限控制在 150 内。"""

    # 页数按关键词的周度产出量分配，而不是所有关键词一视同仁。
    # HKUBS 只给 1 页：实测它第 1 页就开始混入一两年前的旧帖，多翻页只会增加窗口外样本。
    keyword_pool: list[KeywordConfig] = field(
        default_factory=lambda: [
            KeywordConfig(keyword="港大商学院", max_pages=3, max_notes=3 * SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="香港大学商学院", max_pages=2, max_notes=2 * SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="港大经管学院", max_pages=2, max_notes=2 * SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="港大硕士", max_pages=2, max_notes=2 * SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="HKU Business School", max_pages=1, max_notes=SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="HKUBS", max_pages=1, max_notes=SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="港大商科", max_pages=1, max_notes=SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="港大体验", max_pages=1, max_notes=SEARCH_PAGE_SIZE),
            KeywordConfig(keyword="港大就业", max_pages=1, max_notes=SEARCH_PAGE_SIZE),
        ]
    )

    sort: str = "latest_first"
    # 关键词自带 max_pages；这里只作为没有指定页数时的兜底。
    max_pages: int = 1
    time_filter: str = WEEKLY_TIME_FILTER
    reporting_window_days: int = REPORTING_WINDOW_DAYS
    note_type: str = "普通笔记"
    scope_pattern: Optional[str] = HKU_SCOPE_PATTERN
    comment_policy: CommentPolicy = field(default_factory=CommentPolicy)
    output_dir: Optional[str] = None


@dataclass
class Note:
    """归一化后的一条小红书笔记。"""

    note_id: str
    # 保留本次搜索词，方便之后区分“港大 Capstone / 港大 ba”等不同任务。
    keyword: str
    title: Optional[str] = None
    body: Optional[str] = None
    author_id: Optional[str] = None
    author_name: Optional[str] = None
    like_count: Optional[Any] = None
    comment_count: Optional[Any] = None
    collect_count: Optional[Any] = None
    share_count: Optional[Any] = None
    post_url: Optional[str] = None
    published_at: Optional[str] = None
    published_at_raw: Optional[str] = None
    collected_at: Optional[str] = None
    is_valid: bool = True
    is_scope_relevant: bool = True
    skip_reasons: list[str] = field(default_factory=list)
    raw: Optional[dict[str, Any]] = None  # search/detail 原始响应，便于回溯字段。

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Comment:
    """归一化后的一条小红书评论。"""

    note_id: str
    comment_id: str
    content: str
    parent_comment_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    like_count: Optional[Any] = None
    published_at: Optional[str] = None
    raw: Optional[dict[str, Any]] = None  # 保留 TikHub 原始评论节点。

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionRun:
    """一次采集运行的内存结果；最终会落到 collection.json/JSONL。"""

    keyword: str
    run_dir: str
    notes: list[Note] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    api_request_count: int = 0
