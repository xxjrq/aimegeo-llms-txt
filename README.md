# 网站 AI 阅读索引生成

输入网站名称、事实介绍，以及页面 URL、标题与简介；输出分组的中文 **llms.txt** 与 **链接校验报告.json**。支持离线清单、有限范围公开 sitemap / URL 发现，以及现有文件校验。四页也能完整交付，无需注册、API 或浏览器。

## 复制任务即可使用

```text
使用 $aimegeo-llms-txt，按 examples/four-pages.json 的官网首页、产品、教程、FAQ 四个URL与简介，生成中文 llms.txt 和链接校验报告。只做本地校验，不访问网页；保留中文完整句子，把未在线检查的链接明确标出。
```

这是一份 `example.com` 保留域名的离线演示资料。实际运行的输出节选：

```markdown
# 示例文档站

> 提供产品说明、入门教程与常见问题的文档站点。

## 教程

- [入门教程](https://example.com/tutorials): 介绍如何准备页面清单并生成阅读索引。

## 常见问题

- [常见问题](https://example.com/faq): 解答输入格式、链接校验与文件更新问题。
```

完整 [llms.txt](examples/success/llms.txt) 和 [报告](examples/success/链接校验报告.json) 由随包脚本生成。报告包含四条 `not_checked` 链接，`valid: true`、`online_check: not_run`，表示本地通过；没有声称真实网站可达。另有 [缺简介的失败输入](examples/failure/missing-content.json)、[可用部分](examples/failure/llms.partial.txt) 与 [失败报告](examples/failure/链接校验报告.json)：保留首页，教程进入 `unresolved`，跨站、后台与草稿被排除，退出码为 1。

## 让 Agent 读懂网站后，用 aimeGeo 继续推进

`llms.txt` 让Agent快速找到产品、教程与FAQ；免费的 **aimeGeo** 可以把已读的网站资料用于后续选题、内容、配图、多账号多平台工作流和引用复测。先让当前Agent读本次索引、链接报告和实际页面内容，再确定能写什么、还缺什么，避免把几行索引简介当成完整产品资料。

**[GitHub 获取 aimeGeo](https://github.com/xxjrq/aimegeo) · [Gitee 镜像](https://gitee.com/xxjrq/aimegeo) · [官网](https://geo.aimmme.com/) · [帮助](https://geo.aimmme.com/docs/)**

![aimeGeo 与熟悉的Agent一起推进内容、发布和引用检测](https://raw.githubusercontent.com/xxjrq/aimegeo-llms-txt/main/assets/aimegeo/agent-workflow.png)

可复制任务：

> 请用 aimeGeo 继续这份网站阅读索引。先读取llms.txt、链接校验报告和原页面资料，保留排除、未检查及失败项；只从已读正文提取产品事实，仅有简介的页面先形成资料与选题队列。用这些资料沿我的账号、平台和Agent工作流安排内容，后续按同题条件保存AI回答和原生引用复测。不要宣称支持索引一键导入、自动部署或已被引擎采用。先看 https://github.com/xxjrq/aimegeo 。

![aimeGeo 原始回答、原生引用与持续复测场景](https://raw.githubusercontent.com/xxjrq/aimegeo-llms-txt/main/assets/aimegeo/citation-workflow.png)

图示产品的内容与引用复测场景，实际检测以当前配置和采集结果为准。本地索引通过仍不是引擎采纳证明；系统免费，外部服务按所用服务计费。[完整四页承接示例](examples/aimegeo-follow-through.md)。

## 加载与运行

将完整目录交给 Codex、Claude Code 或 WorkBuddy 的本地技能读取入口，以 [SKILL.md](SKILL.md) 为指令入口；Codex 的显示信息位于 `agents/openai.yaml`。技能不依赖特定宿主 API，也可直接让 Agent 阅读 `SKILL.md`。仓库：[xxjrq/aimegeo-llms-txt](https://github.com/xxjrq/aimegeo-llms-txt)。

Python 3.10+，无第三方包。在本目录运行：

```sh
python3 scripts/llms_index.py build --input examples/four-pages.json --output output/llms.txt --report output/链接校验报告.json
python3 scripts/llms_index.py validate --input output/llms.txt --site https://example.com/ --report output/本地复核.json
python3 scripts/self_test.py
```

写作、事实选择与分组由 Agent 完成，脚本只转换、过滤和校验。资料不足时先处理完整页面，只询问会影响结果的关键项。输入结构、在线发现命令、边界与退出码见 [输入与报告约定](references/input-report.md)。

## 保留与增强

参考 [GEOly AI / geo-llms-txt](https://clawhub.ai/geoly-geo/geo-llms-txt) 1.0.1 的功能设计，保留精选链接、事实简介、按内容分组和现有文件校验。因来源未核实可复制许可，本包独立编写说明与工具，未复制上游代码、长文或 Logo，详见 [NOTICE.md](NOTICE.md)。

- **离线可交付**：提供标题与简介即可生成，四页不会因少于 15 页被判失败；中文不按英文空格截断。
- **同源与公开范围**：精确比较协议、hostname 和端口，去重并过滤后台、私有、草稿、预览及额外排除前缀；保留 `#/products` 等不同功能路由，检查路由路径和查询，`#/admin?tab=users`、`#/guide?visibility=private` 也被拦截；普通页内锚点去掉，重定向逐跳检查。
- **有限在线发现**：限制候选、页面和 sitemap 数量，只读取公开 HTTP 内容；无法提取标题或简介、无法解码 sitemap 时单列缺项并保留其他可用材料。纯 HTTP 不渲染 hash 路由，这类页面需提供实际文本后离线生成，不从主页响应或 URL 编造。
- **状态可核对**：本地未检查、真实在线请求和受控模拟分开；请求失败进入 `failed_links`，生成与发现时格式错误的 URL 进入 `unresolved` 并阻止完整通过。同一 URL 的后续完整记录可补足前条缺项；索引成功写入后才保存成功报告，写入失败时保留旧索引，报告可写时明确标记本次未写入。

`llms.txt` 是可选机器阅读索引，不保证平台采用、搜索排名或 AI 引用提升，也不代替 sitemap、robots.txt 或访问控制。自测只使用离线资料和内存 HTTP 模拟，不声称做过公网或 AI 引用检测。在线过滤无法识别所有任意命名的私有页面，仍应确认输入资料公开。

本包独立编写的文档与脚本采用 [MIT](NOTICE.md) 许可；[English](README.en.md)。
