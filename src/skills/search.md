---
name: 精准搜索
description: 根据用户需求进行搜索，优先从 bilibili、知乎、贴吧、腾讯新闻、arxiv 获取结果，没有匹配的再扩大范围
tools: web_search, fetch_url
---

# 精准搜索

## 搜索策略：分层递进

### 第一轮：定向搜索（必须执行）

根据用户问题内容，选择最相关的 2-3 个平台，使用 `site:` 限定搜索：

- **bilibili (B站)**：适合教程、测评、Vlog、ACG 相关内容 → `site:bilibili.com`
- **知乎**：适合观点讨论、经验分享、深度问答 → `site:zhihu.com`
- **贴吧**：适合小众话题、用户讨论、兴趣圈子 → `site:tieba.baidu.com`
- **腾讯新闻**：适合时事新闻、社会热点、政策动态 → `site:news.qq.com`
- **arxiv**：适合学术论文、技术研究、前沿科学（英文搜索） → `site:arxiv.org`

每个定向搜索使用一次 `web_search`，关键词格式：
```
「用户问题核心关键词」 site:目标域名
```

示例：
- 用户问"Python 异步编程最佳实践" → 搜 `Python 异步编程 site:zhihu.com`、`Python async site:arxiv.org`
- 用户问"最新手机推荐" → 搜 `手机推荐 2026 site:bilibili.com`、`手机推荐 site:zhihu.com`
- 用户问"某游戏攻略" → 搜 `游戏名 攻略 site:bilibili.com`、`游戏名 攻略 site:tieba.baidu.com`

### 第二轮：内容抓取

对第一轮搜索结果中**最有价值的 2-3 条**用 `fetch_url` 打开获取全文。

### 第三轮：扩大搜索（仅在第一轮无满意结果时）

如果定向搜索没有找到足够好的内容，去掉 `site:` 限定，用相同关键词进行**通用搜索**（不再限定平台）。

## 注意事项

- 第一轮搜索时，不同平台的搜索可以并行执行（多轮 tool calling）
- 控制总搜索次数在 5 轮以内，留出轮次给 fetch_url
- 如果 arxiv 返回英文论文，可以简要翻译核心观点
- bilibili 和贴吧的搜索结果如果只是索引页而非内容页，优先用 fetch_url 打开具体的帖子/视频页面
- 最终回答要注明信息来源（哪个平台）
