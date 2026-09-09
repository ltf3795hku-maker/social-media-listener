"""Prompt templates for analysis-stage LLM calls."""
from __future__ import annotations

import json
from typing import Any


BROAD_POST_ANNOTATION_FIELDS = """
请为每条已通过 HKU Relevance Gate 的小红书笔记输出以下字段：

- note_id: string，必须与输入一致。
- hku_relevance: "direct" | "indirect"，必须原样复制 relevance_gate.hku_relevance，不要重新判断。
- hku_relevance_reason: string，必须原样复制 relevance_gate.hku_match_reason 或 relevance_reason。

- content_type: "complaint" | "concern" | "question" | "information_sharing" | "positive_advocacy" | "other"
  判断这条内容的表达功能。
  特别注意：若帖子标题或开头看似在抱怨（如“别来HKU了”、“HKU避坑”），但正文实际在理性解释原因、分享攻略或传授经验，必须判定为 "information_sharing"。

- theme: "Admissions" | "Course_Selection" | "Teaching_Quality" | "Academic_Workload" | "Programme_Experience" | "Career_Outcomes" | "Internships" | "Student_Services" | "Accommodation" | "Campus_Life" | "Scholarships" | "Reputation" | "Other"
  可选辅助字段，判断这条内容大致属于哪个 HKU 讨论洞察主题。不要为了分类而硬塞；不明确时填 Other。

- sentiment: "positive" | "neutral" | "negative"
  判断整体情绪倾向。
  反串文辨析：警惕“欲扬先抑”或“反串（讽刺/标题党）”文本。如果作者表面使用负面词汇（如避坑、劝退、大无语），但其实际目的是为了科普背景、客观解释政策成因或进行干货分享，其情绪应判定为 "neutral"（中性客观解释）或 "positive"（变形的正面安利），而非 "negative"。

- author_type: "real_user" | "agency_marketing" | "unclear"
  默认填 unclear。只有强证据才填 agency_marketing，例如私信/vx/微信/工作室/中介/咨询/资料包/保录/规划/留学顾问/付费服务/明显引流话术，或 signal_types 含 commercial。
  只有明显自述真实经历、非引流表达、学生/申请者/校友语境清楚时才填 real_user。宁可漏判营销，也不要把真实学生误判为营销号。

- signal_label: string
  20字以内，用一句具体、可读的短语概括这条帖子的主要信号。
  不要使用泛泛标签，例如“申请问题”“选课问题”“学生体验”。

- risk_level: "high" | "medium" | "low" | "none"
  只基于证据判断风险/紧急程度；不要理解为处理优先级，不暗示 owner/action/timeframe。

- risk_type: "misunderstanding" | "cost_concern" | "decision_impact" | "reputation" | "operational" | "service_friction" | "none"
  只选择一个最主要的风险类型；没有明确风险时填 none。

- risk_reason: string
  80 字以内说明 risk_level 的证据依据；risk_level=none 时可填空字符串。

- signal_types: string[]，可多选，值来自 "operational" | "informational" | "emotional" | "identity" | "commercial" | "other"
  只根据文本表层证据分类，不推断作者真实心理。

- evidence_quote: string
  从原文中摘取支持 signal_label 的短证据。必须是原文或评论样本中的直接表达，不要自己改写。
  如果没有明确证据，填空字符串。

- 不要输出 discussion_topic、subtopic_label、primary_narrative、owner、action、recommendation。
- 输入不会包含 hku_relevance=unrelated 的帖子；若意外出现 unrelated，只输出 note_id/hku_relevance/hku_relevance_reason，其他业务字段填 null、空字符串或空数组，不继续分析。
"""


TOPIC_POST_ANNOTATION_FIELDS = """
请为每条已通过 Topic Relevance Gate 的小红书笔记输出以下字段：

- note_id: string，必须与输入一致。
- topic_relevance: "direct" | "indirect"，必须原样复制 relevance_gate.topic_relevance，不要重新判断。
- relevance_reason: string，必须原样复制 relevance_gate.relevance_reason。
- content_type: "complaint" | "concern" | "question" | "information_sharing" | "positive_advocacy" | "other"。
- sentiment: "positive" | "neutral" | "negative"。
- primary_narrative: string，20字以内，概括这条帖子代表的主要市面说法。
- narrative_labels: string[]，最多3个，使用自然、具体的短标签。
- narrative_stance: "positive" | "negative" | "mixed" | "neutral"。
- has_uncertainty: boolean，帖子是否包含明确提问、不确定、误解或待确认信息。
- uncertainty_type: string | null，例如“项目要求”“录取标准”“成本”“流程”“时间安排”；没有则 null。
- uncertainty_text: string | null，80字以内概括用户在问什么或不确定什么；没有则 null。
- author_type: "real_user" | "agency_marketing" | "unclear"。默认 unclear；只有明确私信/vx/工作室/中介/资料包/咨询引流等强证据才标 agency_marketing。
- signal_label: string，20字以内，概括可聚合的具体信号。Topic 中该字段只作为证据辅助，不作为报告主结构。
- evidence_quote: string，摘取原文短句，不得改写或编造。

只输出上面列出的字段；不要输出任何未列字段。
输入不会包含 topic_relevance=unrelated 的帖子；若意外出现 unrelated，只输出 note_id/topic_relevance/relevance_reason，其他业务字段填 null、空字符串、空数组或 false，不继续分析。
"""


RELEVANCE_FIELDS = """
请只判断每条小红书笔记的主题相关性，输出以下字段：

- note_id: string，必须与输入一致。
- topic_relevance: "direct" | "indirect" | "unrelated"
- relevance_reason: string，30字以内，说明相关或不相关的依据。

不要输出 theme、sentiment、signal_label、content_type 等业务标签。
不要修改输入里的 hku_relevance；HKU 相关性已由代码规则判断。
"""


SIGNAL_TYPE_RULES = """
signal_types 可以多选，但只能根据文本表层证据分类，不推断作者真实心理：
- operational: 系统、流程、容量、排课、waiting list、登录、选课机制、课程安排。
- informational: 攻略、经验、信息差、避坑、步骤、选课知识。
- emotional: 明确出现焦虑、崩溃、压力、害怕、后悔、开心、安心等情绪表达。
- identity: 主要展示身份、项目归属、offer、名校标签、同侪比较，而非解决具体问题。
- commercial: 引流、服务、咨询、私信、课程、代办、资料包、付费转化等营销线索。
- other: 证据不足或不属于以上；不确定时只选 other。
"""


COMMON_ANALYSIS_RULES = """
证据纪律：
- 只使用输入数据，不引入外部信息；每个 insight 必须能追溯到输入聚合表或有限帖子/评论证据。
- 没有证据的模块返回空数组 []，不要为填满字段创造 insight。
- 商业 / 推广 / 中介 / 资料包 / 私信引流内容不要等同于真实学生情绪；如影响解释，写入 author_type_summary 或 data_limitations。
- 不要硬过滤 author_type；默认 unclear，只有强证据才说 agency_marketing。
- 评论不是独立语料：Broad 评论结合 post_title/post_theme/post_signal；Topic 评论结合 post_title/post_primary_narrative/post_signal；上下文不足时只作为该帖的补充信号。

反串与意图辨析（核心）：
- 必须准确识别社媒中“欲扬先抑”或“标题党反串”的干货分享帖。
- 若帖子表面使用“劝退”、“避坑”等负面词汇，但正文实际是在理性解释原因、分享机制、消除信息差，其 content_type 必须为 information_sharing，且 sentiment 应为 neutral 或 positive，绝不可仅凭表层词汇误判为 negative 负面情绪或 high 风险。

数字纪律：
- 你不输出任何统计数字；volume、mention_count、engagement、recent_7d_count、latest_post_date 等
  全部由代码从聚合表填充。正文叙述中如需引用数字，只能照抄对应代码聚合表里的值。

写作纪律：
- 不要输出 owner、suggested_action、recommended_content、timeframe、how_to_use、which team should respond。
- findings 是跨主题的宏观观察；alerts 是具体的负面/敏感讨论信号，二者不要重复。
- 避免空泛表述（提升学生体验、加强沟通机制、值得关注）；executive_summary 用具体观察结论，不用咨询式套话。
- 全部面向读者的内容用中文；必要英文专名可保留。
- JSON key 和枚举值按 schema 保持英文，但 summary、takeaway、finding、evidence 等读者文本不得直接展示内部字段名、snake_case 标签或英文主题枚举。
- 在中文正文中必须写“招生与录取、项目体验、奖学金与费用”等中文主题名，不写 Admissions、Programme_Experience、Scholarships；不写 hku_relevance=direct/indirect 等技术标签。

时间纪律：
- published_at 是发布时间；collected_at 是采集时间，不能用于判断信号新旧。
- 信号活跃度只引用聚合表的 recent_7d_count / latest_post_date / earliest_post_date。
- time_coverage 给出缺发布时间的帖子数；notes_without_publish_date > 0 时必须写入 data_limitations。
""".strip()


TOPIC_ANALYSIS_RULES = """
证据纪律：
- 只使用输入数据，不引入外部信息；每个结论必须能追溯到 narrative_table、uncertainty_table、narrative_comment_table 或有限帖子证据。
- 没有证据的模块返回空数组 []，不要为填满字段创造内容。
- 商业 / 推广 / 中介 / 资料包 / 私信引流内容不要等同于真实学生情绪；如影响解读，只写入 data_limitations。
- 评论不是独立语料；Topic 评论必须结合 post_primary_narrative / post_signal，上下文不足时只作为该帖的补充信号。

数字纪律：
- 你不输出任何自行统计的数字；volume、engagement、comment_count、question_count、latest_post_date 等全部来自代码聚合表。
- 正文叙述中如需引用数字，只能照抄对应代码聚合表里的值。

写作纪律：
- 只回答四件事：发生了什么；大家主要怎么说；评论里如何反应或追问；还有哪些信息不清楚。
- 只呈现样本内可核验事实；不写“这意味着什么”、管理含义、影响推断、行动建议或推荐。
- 全部面向读者的内容用中文；必要英文专名可保留。
- JSON key 和枚举值按 schema 保持英文，但 summary、evidence 等读者文本不得直接展示内部字段名、snake_case 标签或英文主题枚举。
- 避免空泛表述；executive_summary 用具体观察结论，不用咨询式套话。

时间纪律：
- published_at 是发布时间；collected_at 是采集时间，不能用于判断信号新旧。
- time_coverage 给出缺发布时间的帖子数；notes_without_publish_date > 0 时必须写入 data_limitations。
""".strip()


BROAD_MODE_SECTION = """
本次是 HKU/HKUBS 周度社媒监测报告，目标是呈现本轮品牌讨论全景。
回答“本轮 HKU/HKUBS 整体讨论集中在哪里、各主题主要在说什么、跨主题有哪些重要发现、
哪些负面或敏感讨论需要关注，以及有哪些明确的正面声誉信号”；
只围绕 hku_relevance 为 direct/indirect 的讨论。
必须以 theme_table 的固定主题分类作为“话题分布”主结构。

Alert 指本轮社媒监测中值得关注的讨论信号（负面、敏感、投诉、担忧、费用、招生录取、
服务摩擦、运营问题、明显误解或信息混乱）。
Alert 是监测关注优先级，不等同于已确认的现实风险，也不代表现实事件的严重程度。

输出字段（JSON object，不要输出未列出的字段）：
{
  "executive_summary": "string，200字以内，总结本轮主要讨论重心、最值得关注的 alerts 和主要正面信号；不要逐项复述所有模块",
  "theme_landscape": [{"theme": "固定 taxonomy 值", "summary": "string", "evidence": "string"}],
  "key_findings_across_themes": [{"finding": "string", "summary": "string", "supporting_themes": ["theme_table.theme 的原值"], "supporting_signal_ids": ["signal_table.signal_id 的原值"], "evidence": "string"}],
  "alerts": [{"signal_id": "string，原样复制 alert_table.signal_id", "signal": "string，原样复制同一条的 alert_table.signal", "alert_level": "string，原样复制同一条的 alert_table.alert_priority", "alert_type": "string，原样复制同一条的 alert_table.alert_type", "summary": "string", "comment_signal": "string，基于 comment_signal_table / narrative_comment_table 概括该信号下一级评论的补充；没有评论则空字符串", "evidence": "string", "supporting_quotes": [{"quote": "30-80字原话", "note_id": "string"}]}],
  "positive_reputation_signals": [{"signal_id": "string，原样复制 positive_signal_table.signal_id", "signal": "string，原样复制同一条的 positive_signal_table.signal", "summary": "string，说明该正面讨论来自哪些实际体验、事件或观点", "comment_signal": "string，同上；没有评论则空字符串", "evidence": "string"}],
  "appendix": {"supporting_evidence": [{"source": "string", "evidence": "string"}], "methodology": ["string"]},
  "data_limitations": ["string"]
}

模块规则：
- theme_landscape 只输出 theme_table 中实际出现的主题；数字由代码回填。
- key_findings_across_themes 必须跨至少两个 theme_table 中真实存在的主题；supporting_themes 原样复制
  theme_table.theme，supporting_signal_ids 只能填 signal_table.signal_id；只能证明单个 theme 内部现象时不要输出；
  不重复逐个 theme 摘要，也不写建议或行动项。
- alerts 只能来自 alert_table：每条对应且只对应一个 alert_table.signal_id，不得新增、合并、重复或改写等级；
  alert_table 为空时输出 []。sentiment=negative 或高互动本身都不是新增 alert 的理由，是否进入 alerts 已由代码决定。
- alert 的 summary 只写数据支持的事实：发生了什么、用户主要在担心/抱怨/质疑/追问什么、该信号在本轮样本中的表现。
  禁止推演证据未直接讨论的后果（例如影响心理健康、招生表现、学校竞争力、申请决策质量、品牌受损），
  也不要用“危机、严重损害、重大影响”这类夸张定性。
- positive_reputation_signals 只能来自 positive_signal_table，同样每条对应一个 signal_id，不得新增或合并；
  不得把营销/中介表述当成真实学生口碑，也不得从单条正面帖子推演整体品牌声誉提升；不写建议或负责人。
- 评论不是独立章节；如 comment_signal_table / narrative_comment_table 有对应评论，
  只在该条的 comment_signal 中概括一级评论如何附和、质疑、追问或补充。
""".strip()


TOPIC_MODE_SECTION = """
本次是 Topic Search 报告，目标是深入分析一个具体主题。
回答"发生了什么、大家主要怎么说、评论里如何反应或追问、还有哪些信息不清楚"；
只围绕 topic_relevance 为 direct/indirect 的完整搜索主题相关讨论，不要把只相关 HKU 但不相关 topic 的内容写进专题洞察。
正文主结构必须使用 narrative_table；primary_narrative 是报告骨架，signal_label 只作为辅助证据字段。

输出字段（JSON object，不要输出未列出的字段）：
{
  "executive_summary": "string，200字以内，一段话总结该关键词下的整体讨论",
  "main_narratives": [{"cluster_id": "string，必须原样复制 narrative_table.cluster_id", "narrative": "string", "stance": "positive | negative | mixed | neutral", "summary": "string", "comment_signal": "string，基于 narrative_comment_table 概括该叙事下一级评论的反应/追问/补充；没有评论则空字符串", "evidence": "string"}],
  "questions_uncertainties": [{"uncertainty_id": "string，必须原样复制 uncertainty_table.uncertainty_id", "question": "string", "uncertainty_type": "string", "summary": "string", "evidence": "string"}],
  "appendix": {"supporting_evidence": [{"source": "string", "evidence": "string"}], "methodology": ["string"]},
  "data_limitations": ["string"]
}

模块规则：
- main_narratives 是报告核心，覆盖该 topic 下市面主要说法，正面、负面、中性都要纳入；stance 必填，正负面解读就体现在每条的 stance 上，不再单列正负面段落。
- 每条 main_narrative 必须原样复制一个 narrative_table.cluster_id；不得合并多个 cluster_id，也不得改变代码聚合的 cluster 边界。
- 只写 narrative_table 中有证据的说法，不要把单帖标题当 narrative。
- main_narratives 应互相排斥，通常 3-6 条即可；同一对象、同一事件或同一体验下的多个侧面应合并为一条更宽的 narrative，并在 summary 里说明差异。
- 标题和 summary 使用中性描述，避免夸张措辞；把强定性表达改写为更可解释的描述。
- 评论不是独立章节；如 narrative_comment_table 对应叙事有评论，只在该 narrative 的 comment_signal 中概括一级评论如何附和、质疑、追问或补充帖子侧说法。
- questions_uncertainties 需要同时参考 uncertainty_table 和 narrative_comment_table.question_comments；评论里集中追问的问题也算信息不确定点。
- 每条 questions_uncertainties 必须原样复制一个 uncertainty_table.uncertainty_id；不得把同一个类型的总数套到不同问题上。
- questions_uncertainties 只写中性的信息缺口/待澄清问题：大家在问什么、容易误解或官方说明不清的地方。
- 负面信息只作为 narrative 的 stance 和 summary 呈现。
""".strip()


def build_broad_analysis_prompt(
    *,
    processing: dict[str, Any],
    time_coverage: dict[str, Any],
    top_posts: list[dict[str, Any]],
    signal_table: list[dict[str, Any]],
    alert_table: list[dict[str, Any]],
    positive_signal_table: list[dict[str, Any]],
    theme_table: list[dict[str, Any]],
    discussion_table: list[dict[str, Any]],
    comment_signal_table: list[dict[str, Any]],
    narrative_comment_table: list[dict[str, Any]],
    sample_comments: list[dict[str, Any]],
) -> str:
    return f"""
你是 HKU RED Insights Analysis Agent。
请基于帖子标注、帖子内容、代码聚合表和评论样本，输出可直接用于 HKU RED insights report 的结构化 JSON。
报告服务于 HKU Faculty / Programme Team / Marketing / Student Services 等内部团队决策。

{COMMON_ANALYSIS_RULES}

{BROAD_MODE_SECTION}

annotation 字段解释：
Broad：theme、content_type、sentiment、author_type、signal_types、risk_level/risk_type、signal_label、evidence_quote。

处理摘要：
{json.dumps(processing, ensure_ascii=False)}

time_coverage（代码统计，只可引用）：
{json.dumps(time_coverage, ensure_ascii=False)}

帖子样本：
{json.dumps(top_posts, ensure_ascii=False)}

代码聚合 signal_table（全部信号；数字只可引用，不可改写）：
{json.dumps(signal_table, ensure_ascii=False)}

代码聚合 alert_table（alerts 的唯一候选来源；不得新增、合并或改变等级）：
{json.dumps(alert_table, ensure_ascii=False)}

代码聚合 positive_signal_table（positive_reputation_signals 的唯一候选来源；不得新增或合并）：
{json.dumps(positive_signal_table, ensure_ascii=False)}

代码聚合 discussion_table（Broad 为 theme 辅助表，数字只可引用，不可改写）：
{json.dumps(discussion_table, ensure_ascii=False)}

代码聚合 theme_table（Weekly Monitoring 的“话题分布”主结构，数字只可引用，不可改写）：
{json.dumps(theme_table, ensure_ascii=False)}

代码聚合 comment_signal_table（数字只可引用，不可改写）：
{json.dumps(comment_signal_table, ensure_ascii=False)}

代码聚合 narrative_comment_table（一级评论按所属帖子主题/叙事聚合；数字只可引用，不可改写）：
{json.dumps(narrative_comment_table, ensure_ascii=False)}

评论样本：
{json.dumps(sample_comments, ensure_ascii=False)}

只输出 JSON object，不要解释。
"""


def build_topic_analysis_prompt(
    *,
    processing: dict[str, Any],
    time_coverage: dict[str, Any],
    post_evidence: list[dict[str, Any]],
    narrative_table: list[dict[str, Any]],
    uncertainty_table: list[dict[str, Any]],
    narrative_comment_table: list[dict[str, Any]],
) -> str:
    return f"""
你是 HKU RED Topic Insights Agent。
请基于 Topic 相关帖子、代码聚合表和一级评论聚合信号，输出可直接用于 Topic Report 的结构化 JSON。
报告目标是给内部读者说明：发生了什么、大家主要怎么说、评论里怎么反应、还有哪些信息不清楚。

{TOPIC_ANALYSIS_RULES}

{TOPIC_MODE_SECTION}

annotation 字段解释：
Topic：content_type、sentiment、primary_narrative、narrative_labels、narrative_stance、
has_uncertainty/uncertainty_type/uncertainty_text、author_type、signal_label、evidence_quote。

处理摘要：
{json.dumps(processing, ensure_ascii=False)}

time_coverage（代码统计，只可引用）：
{json.dumps(time_coverage, ensure_ascii=False)}

有限帖子证据（只用于写作证据和理解上下文，不重新计数）：
{json.dumps(post_evidence, ensure_ascii=False)}

代码聚合 narrative_table（Topic 主结构；数字只可引用，不可改写）：
{json.dumps(narrative_table, ensure_ascii=False)}

代码聚合 narrative_comment_table（一级评论按所属帖子 primary_narrative 聚合；数字只可引用，不可改写）：
{json.dumps(narrative_comment_table, ensure_ascii=False)}

代码聚合 uncertainty_table（帖子侧问题/不确定点；需与 narrative_comment_table.question_comments 一起判断）：
{json.dumps(uncertainty_table, ensure_ascii=False)}

只输出 JSON object，不要解释。
"""
