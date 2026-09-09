"""Pure, local-only fixtures for the Streamlit UI preview mode.

This module deliberately has no Streamlit, service, database, network, or file
dependencies.  The production app imports it only when UI_PREVIEW_MODE is
explicitly enabled.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


PREVIEW_STATES = (
    "Home",
    "Searching",
    "Search Completed / Processing",
    "Analyzing",
    "Analysis Completed / Generating Report",
    "Final Report",
    "No Results",
    "Error",
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_CLOUD_ENV_KEYS = (
    "WEBSITE_INSTANCE_ID",
    "WEBSITE_SITE_NAME",
    "WEBSITE_HOSTNAME",
)


def preview_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Enable preview only by opt-in and never inside Azure App Service."""

    values = os.environ if env is None else env
    requested = str(values.get("UI_PREVIEW_MODE", "false")).strip().lower() in _TRUE_VALUES
    running_in_azure = any(str(values.get(key, "")).strip() for key in _CLOUD_ENV_KEYS)
    return requested and not running_in_azure


def build_preview_posts(count: int = 50) -> list[dict[str, Any]]:
    """Return deterministic, realistic-looking RED post rows for UI rendering."""

    templates = (
        (
            "港大选课避坑：热门课候补到底怎么排",
            "整理了 add/drop period 的实际体验，热门课需要尽早加入候补名单，也要准备两到三个备选方案。",
            "热门课候补规则",
            "negative",
        ),
        (
            "HKU 商学院选课攻略｜新生先看培养方案",
            "建议先核对必修、选修和毕业学分，再按时间冲突安排课表。部分课程每学期开放情况不同。",
            "培养方案与学分规划",
            "neutral",
        ),
        (
            "选课系统开放第一天，我的真实体验",
            "系统整体能用，但高峰时段加载较慢。提前收藏课程代码会方便很多，最后成功选到两门目标课。",
            "系统高峰体验",
            "mixed",
        ),
        (
            "课程 workload 怎么判断？学长姐经验汇总",
            "不能只看课程名称，考核比例、小组作业和期末安排更重要。分享几门课的时间投入供参考。",
            "课程工作量判断",
            "neutral",
        ),
        (
            "跨学院选修值不值得",
            "跨学院课程选择更多，但要留意先修要求、名额限制以及是否计入本项目毕业学分。",
            "跨学院选修与学分认定",
            "positive",
        ),
        (
            "求问：waive 课程会不会影响专业认证",
            "已经满足部分基础课要求，但不确定申请课程豁免后是否影响认证路径和后续选修安排。",
            "课程豁免与专业认证",
            "neutral",
        ),
        (
            "港大选课时间表和关键节点整理",
            "把预选、正式选课、候补和退改课时间放在一张表里，新生按节点准备就不容易漏掉。",
            "选课时间与流程",
            "positive",
        ),
        (
            "热门课没选上，还有哪些替代课程",
            "根据课程方向整理了几组替代选择，内容侧重相近，但授课时间和考核方式差异比较明显。",
            "热门课替代方案",
            "neutral",
        ),
    )
    posts: list[dict[str, Any]] = []
    for index in range(count):
        title, body, narrative, stance = templates[index % len(templates)]
        relevant = index < 37
        posts.append(
            {
                "note_id": f"preview-note-{index + 1:02d}",
                "status": "kept" if relevant else "excluded",
                "title": title if index < len(templates) else f"{title}（样本 {index + 1}）",
                "body_preview": body,
                "post_url": f"https://www.xiaohongshu.com/explore/{index + 1:024x}",
                "like_count": 128 + ((index * 83) % 1900),
                "collect_count": 31 + ((index * 47) % 620),
                "comment_count": 8 + ((index * 19) % 180),
                "share_count": 3 + ((index * 11) % 95),
                "is_scope_relevant": relevant,
                "hku_relevance": "direct" if relevant else "unrelated",
                "topic_relevance": "direct" if index < 29 else "indirect" if relevant else "unrelated",
                "content_type": "question" if index % 5 == 0 else "information_sharing",
                "primary_narrative": narrative if relevant else "",
                "narrative_stance": stance if relevant else "",
                "has_uncertainty": bool(relevant and index % 5 == 0),
                "signal_label": narrative if relevant else "",
            }
        )
    return posts


def build_preview_analysis(posts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return aggregate analysis data matching the production run UI contract."""

    rows = posts or build_preview_posts()
    evidence = [row for row in rows if row.get("is_scope_relevant")][:5]
    narratives = (
        ("热门课候补与名额竞争", 11, 12840, "negative"),
        ("培养方案与学分规划", 9, 8160, "neutral"),
        ("课程工作量与考核方式", 7, 6940, "mixed"),
        ("课程豁免与专业认证", 6, 5210, "neutral"),
        ("跨学院选修与替代方案", 4, 3760, "positive"),
    )
    narrative_table = []
    for index, (label, volume, engagement, stance) in enumerate(narratives):
        source = evidence[index]
        narrative_table.append(
            {
                "cluster_id": f"preview-cluster-{index + 1}",
                "label": label,
                "volume": volume,
                "engagement_sum": engagement,
                "narrative_stance_counts": {stance: volume},
                "evidence_items": [
                    {
                        "quote": source["body_preview"],
                        "note_id": source["note_id"],
                        "post_url": source["post_url"],
                        "post_title": source["title"],
                        "date": f"2026-08-{12 + index:02d}",
                        "engagement": engagement,
                    }
                ],
            }
        )
    return {
        "keyword": "港大选课",
        "input_notes": 50,
        "analysis_notes": 37,
        "analysis_comments": 126,
        "report_mode": "topic_report",
        "generated_scope": {
            "earliest_post_date": "2026-07-20",
            "latest_post_date": "2026-08-18",
        },
        "time_coverage": {
            "earliest_post_date": "2026-07-20",
            "latest_post_date": "2026-08-18",
            "notes_with_publish_date": 35,
            "notes_without_publish_date": 2,
        },
        "annotation_summary": {
            "sentiment": {"neutral": 20, "positive": 9, "negative": 8},
            "content_type": {"information_sharing": 18, "question": 10, "concern": 5, "positive_advocacy": 4},
            "author_type": {"real_user": 24, "unclear": 11, "agency_marketing": 2},
            "primary_narrative": {label: volume for label, volume, _, _ in narratives},
            "narrative_stance": {"neutral": 15, "negative": 11, "mixed": 7, "positive": 4},
        },
        "narrative_table": narrative_table,
        "uncertainty_table": [
            {"uncertainty_type": "候补名单排序与释放时间", "count": 8, "support_count": 8},
            {"uncertainty_type": "课程豁免对专业认证的影响", "count": 6, "support_count": 6},
            {"uncertainty_type": "跨学院学分是否计入毕业要求", "count": 5, "support_count": 5},
            {"uncertainty_type": "课程工作量和考核比例", "count": 4, "support_count": 4},
        ],
        "analysis": {
            "executive_summary": "讨论主要集中在热门课名额、学分规划、课程工作量和豁免规则。多数帖子属于经验分享，但候补排序和认证影响仍存在明显信息缺口。",
            "questions_uncertainties": [
                {"question": "候补名单按照什么规则排序？", "uncertainty_type": "候补机制", "score": 0.86},
                {"question": "课程豁免会不会影响专业认证？", "uncertainty_type": "课程豁免", "score": 0.72},
                {"question": "跨学院选修是否全部计入毕业学分？", "uncertainty_type": "学分认定", "score": 0.61},
                {"question": "如何在选课前比较不同课程的 workload？", "uncertainty_type": "课程信息", "score": 0.49},
            ],
        },
    }


def build_preview_run(state: str) -> dict[str, Any]:
    """Return a run-like dictionary matching the requested UI phase."""

    status_by_state = {
        "Searching": ("running", "collect"),
        "Search Completed / Processing": ("running", "process"),
        "Analyzing": ("running", "analyze"),
        "Analysis Completed / Generating Report": ("running", "report"),
        "Final Report": ("succeeded", "report"),
        "No Results": ("failed", "collect"),
        "Error": ("failed", "analyze"),
    }
    status, step = status_by_state.get(state, ("queued", "collect"))
    error = ""
    if state == "No Results":
        error = "本轮没有找到符合‘港大选课’搜索条件的公开帖子。"
    elif state == "Error":
        error = "分析服务暂时不可用。请稍后重试；已采集的 50 条帖子仍然保留。"
    return {
        "id": 9001,
        "mode": "topic_scan",
        "status": status,
        "current_step": step,
        "run_dir": "",
        "config": {"collect_config": {"keyword": "港大选课"}},
        "collect_notes_count": 0 if state in {"Searching", "No Results"} else 50,
        "collect_comments_count": 126 if state in {"Analyzing", "Analysis Completed / Generating Report", "Final Report"} else 0,
        "report_html": "preview://report" if state == "Final Report" else "",
        "created_at": "2026-08-19T10:05:00+08:00",
        "started_at": "2026-08-19T10:05:02+08:00",
        "finished_at": "2026-08-19T10:08:48+08:00" if state == "Final Report" else "",
        "error": error,
        "events": _preview_events(state),
    }


def _preview_events(state: str) -> list[dict[str, str]]:
    events = [
        {"time": "2026-08-19T10:05:02+08:00", "step": "collect", "message": "collect started"},
        {"time": "2026-08-19T10:06:14+08:00", "step": "collect", "message": "collect finished"},
        {"time": "2026-08-19T10:06:15+08:00", "step": "process", "message": "process started"},
        {"time": "2026-08-19T10:06:16+08:00", "step": "process", "message": "process finished"},
        {"time": "2026-08-19T10:06:17+08:00", "step": "analyze", "message": "analysis start"},
        {"time": "2026-08-19T10:08:19+08:00", "step": "analyze", "message": "Semantic filter notes=50->37"},
        {"time": "2026-08-19T10:08:20+08:00", "step": "report", "message": "report start"},
        {"time": "2026-08-19T10:08:48+08:00", "step": "report", "message": "report finished"},
    ]
    counts = {
        "Searching": 1,
        "Search Completed / Processing": 3,
        "Analyzing": 5,
        "Analysis Completed / Generating Report": 7,
        "Final Report": 8,
        "No Results": 1,
        "Error": 5,
    }
    selected = events[: counts.get(state, 0)]
    if state == "No Results":
        selected.append({"time": "2026-08-19T10:05:40+08:00", "step": "collect", "message": "collect failed: collection produced 0 notes"})
    elif state == "Error":
        selected.append({"time": "2026-08-19T10:07:10+08:00", "step": "analyze", "message": "analyze failed: analysis service unavailable"})
    return selected


def build_preview_report() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return in-memory arguments for the production HTML report renderer."""

    analysis = build_preview_analysis()
    narratives = []
    for row in analysis["narrative_table"]:
        evidence = row["evidence_items"][0]
        stance_counts = row.get("narrative_stance_counts") or {}
        stance = next(iter(stance_counts), "neutral")
        narratives.append(
            {
                "narrative": row["label"],
                "stance": stance,
                "volume": row["volume"],
                "engagement_sum": row["engagement_sum"],
                "summary": {
                    "热门课候补与名额竞争": "高互动讨论集中在候补顺序、放位时间和备选课程安排。",
                    "培养方案与学分规划": "新生普遍先核对必修与毕业学分，再处理时间冲突。",
                    "课程工作量与考核方式": "学生更关注小组作业、考核比例和实际时间投入。",
                    "课程豁免与专业认证": "已有背景的学生关心豁免后的认证与后续选修影响。",
                    "跨学院选修与替代方案": "替代课程丰富，但学分认定和先修要求需要进一步确认。",
                }[row["label"]],
                "comment_signal": "评论区主要补充个人经历，并继续追问具体规则。",
                "comment_count": max(8, row["volume"] * 3),
                "comment_question_count": max(2, row["volume"] // 2),
                "representative_quotes": [{"quote": evidence["quote"], "evidence_id": f"preview-evidence-{len(narratives) + 1}"}],
                "evidence": f"来自 {row['volume']} 条相关帖子，代表来源见附录。",
            }
        )
    structured = {
        "title": "港大选课｜小红书公开讨论洞察",
        "report_mode": "topic_report",
        "generated_scope": {
            "search_query": "港大选课",
            "analysis_notes": 37,
            "analysis_comments": 126,
            "input_notes": 50,
            "input_comments": 126,
            "earliest_post_date": "2026-07-20",
            "latest_post_date": "2026-08-18",
            "comment_collection_status": "collected",
            "engagement_formula": "点赞 + 2×收藏 + 3×评论 + 分享",
        },
        "header_distributions": analysis["annotation_summary"],
        "executive_summary": analysis["analysis"]["executive_summary"],
        "main_narratives": narratives,
        "questions_uncertainties": [
            {
                "question": row["question"],
                "uncertainty_type": row["uncertainty_type"],
                "count": 4 + index,
                "summary": "现有帖子给出了经验性答案，但规则口径仍不完全一致。",
                "representative_quotes": [{"quote": "请问官方有没有更清楚的说明？"}],
                "evidence": "帖子和评论中均出现重复追问。",
            }
            for index, row in enumerate(analysis["analysis"]["questions_uncertainties"])
        ],
        "appendix": {
            "evidence": [
                {
                    "source": row["title"],
                    "date": f"2026-08-{12 + index:02d}",
                    "hint": row["body_preview"],
                    "post_url": row["post_url"],
                }
                for index, row in enumerate(build_preview_posts(5))
            ],
            "methodology": [
                "本页面为本地 UI Preview Mode，所有内容均为 mock data。",
                "正式运行时沿用相同页面组件和报告 renderer，但数据来自真实采集与分析流程。",
                "预览数据不会写入任务数据库或正式报告库。",
            ],
        },
        "data_limitations": [
            "当前为界面预览，不代表真实平台讨论。",
            "Mock 数据仅用于检查排版、组件状态和交互层级。",
        ],
    }
    processing = {
        "keyword": "港大选课",
        "scan_mode": "topic_scan",
        "processed_notes": 50,
        "processed_comments": 126,
        "comment_collection_status": "collected",
    }
    return structured, analysis, processing
