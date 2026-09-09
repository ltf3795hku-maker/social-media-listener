# HKUBS Social Listening

HKUBS Social Listening 是一个面向香港大学经管学院的小红书社媒监测与分析工具。

系统从公开搜索结果中采集帖子，完成清洗去重、相关性筛选、AI 语义分析、聚合和报告生成，并通过 Streamlit 提供网页界面。

## 项目简介

这个仓库面向 project supervisor、future maintainer 和 developer。Marketing Team 通常直接通过部署后的 Streamlit 网站使用系统，因此这里不写完整用户手册。

生产入口是 `streamlit_app.py`。当前 Azure 部署使用 Linux App Service，并通过 `startup.sh` 启动 Streamlit。

## 两种模式

### Topic Search

Topic Search 用于临时分析一个具体话题。用户可以自行输入关键词，例如某个项目、申请政策、学费、就业或其他市场话题。

系统会识别相关帖子，并总结：

- 主要讨论叙事
- 用户观点与情绪
- Questions / Uncertainties
- 代表性证据

适合快速回答“这个具体话题现在大家在说什么”。

### Weekly Monitoring

Weekly Monitoring 对应系统内部的 Broad Mode，用于 HKUBS 固定的周度社媒监测。

系统使用预设的 HKUBS 关键词，对本周公开讨论进行采集和分析，并生成：

- Executive Brief
- Monitoring Overview
- Top Original Posts
- Alerts / 重点关注
- Positive Signals
- Theme Landscape
- Competitor Weekly Top Posts
- Methodology / Limitations

适合每周定期使用。

一次完整的 Weekly Monitoring 通常约需 20 分钟，实际时间会受到帖子数量、评论采集和外部 API 响应速度影响。

## 基本流程

```text
Collect
→ Process
→ Analyze
→ Aggregate
→ Report
→ Streamlit UI
```

Topic Search 会生成 Topic Report；Weekly Monitoring 会额外执行 Top-10 comments 和 competitor weekly post analysis，再生成 Weekly Report。

## 本地启动

建议使用 Python 3.12+。

```bash
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py \
  --server.port 8502 \
  --server.address 127.0.0.1
```

本地 UI 调试可使用 Preview Mode。它只使用 mock data，不调用 TikHub、LLM、数据库或正式报告生成流程：

```bash
UI_PREVIEW_MODE=true python -m streamlit run streamlit_app.py \
  --server.port 8502 \
  --server.address 127.0.0.1
```

## Runtime

Topic Search 的耗时取决于搜索页数、帖子数量、评论设置和外部 API 响应速度。

一次完整的 Weekly Monitoring 通常约需 20 分钟，实际时间会受到帖子数量、评论采集和外部 API 响应速度影响。

## Data / AI Services

系统主要依赖：

- TikHub：公开小红书样本采集
- AI model：相关性判断、语义标注、聚合分析和报告文字生成

常用环境变量见 `.env.example`。真实 API token、login password 和本地数据目录配置不应提交到 Git。

本地默认数据目录是 `data/`；Azure Linux 默认持久化目录是 `/home/data`。可以通过 `XHS_DATA_DIR` 覆盖。

报告页的 PDF 下载由 Playwright/Chromium 从已生成的 `report.html` 渲染并缓存；首次生成可能需要安装浏览器引擎。

## Notes / Limitations

- 报告仅反映本轮公开搜索样本，不代表小红书全平台总体意见。
- 搜索结果和互动数据可能受到平台排序、时间窗口和 API 可用性的影响。
- AI 用于语义理解和文字总结；关键数量、排序、ID 和 evidence grounding 尽可能由代码确定性控制。
- 历史报告以生成时保存的 HTML 为准；当前 renderer 只服务新的报告生成。

## English

### Overview

HKUBS Social Listening is a Xiaohongshu social monitoring and analysis tool for HKU Business School.

It collects public search results, cleans and deduplicates posts, filters relevance, applies AI-assisted semantic analysis, aggregates discussion signals, and generates reports through a Streamlit web interface.

### Two Modes

#### Topic Search

Topic Search is for ad-hoc analysis of a specific keyword or issue, such as a programme, admissions policy, tuition, career topic, or other market discussion.

The report focuses on:

- Main narratives
- Audience opinions and sentiment
- Questions and uncertainties
- Supporting evidence

It is intended to answer: “What are people currently saying about this specific topic?”

#### Weekly Monitoring

Weekly Monitoring corresponds to Broad Mode in the codebase and is designed for recurring HKUBS social monitoring.

It uses a predefined HKUBS keyword set and generates a weekly report covering:

- Executive Brief
- Monitoring Overview
- Top Original Posts
- Alerts
- Positive Signals
- Theme Landscape
- Competitor Weekly Top Posts
- Methodology / Limitations

A full Weekly Monitoring run usually takes around 20 minutes. Actual runtime varies depending on sample size, comment collection, and external API response time.

### Pipeline

```text
Collect → Process → Analyze → Aggregate → Report → Streamlit UI
```

Topic Search runs through public sample collection, cleaning, relevance filtering, AI annotation, topic aggregation, and `report.json` / `report.html` generation.

Weekly Monitoring adds Top-10 comment collection and competitor weekly post analysis before generating the weekly report.

### Local Run

Use the same commands shown in 本地启动. Preview Mode is local-only and uses mock data instead of TikHub, LLM, database writes, or report-library writes.

### Runtime

Topic Search runtime varies with search scope, post count, comment settings, and external API latency.

A full Weekly Monitoring run usually takes around 20 minutes. Actual runtime varies depending on sample size, comment collection, and external API response time.

### Services

The system mainly relies on TikHub for public Xiaohongshu sample acquisition and an AI model for relevance analysis, semantic annotation, aggregation, and report writing.

Environment variables are documented in `.env.example`. API credentials must be configured locally or in the deployment environment and must not be committed to Git.

PDF downloads are rendered and cached from `report.html` with Playwright/Chromium; the first generation may need to install the browser engine.

### Notes

- Reports describe the public search sample collected for that run and should not be interpreted as a complete representation of the platform.
- Search coverage and engagement metrics depend on platform ranking, time windows, and API availability.
- AI is used for semantic interpretation and writing, while key counts, ranking logic, IDs, and grounding are handled deterministically in code where possible.
- Historical reports should be viewed from their saved HTML output; the current renderer is intended for current report generation.
