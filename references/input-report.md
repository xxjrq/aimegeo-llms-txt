# 输入、命令与报告约定

## 离线输入

`build` 接收 UTF-8 JSON；完整四页输入见 [four-pages.json](../examples/four-pages.json)。

```json
{
  "site": {
    "url": "https://example.com/",
    "name": "示例文档站",
    "description": "提供产品说明、入门教程与常见问题的文档站点。"
  },
  "pages": [
    {
      "url": "https://example.com/",
      "title": "首页",
      "description": "汇总产品、教程与常见问题入口。",
      "section": "网站概览",
      "visibility": "public",
      "status": "published"
    }
  ]
}
```

- `site.url/name/description` 必填；可选 `overview` 只能放已有事实，无资料就省略。
- `pages` 最多 5000 条。`url/title/description` 必填；相对 URL 基于 `site.url` 解析。格式错误的 URL（如无效端口）进入 `unresolved`，不会当作正常排除项。缺标题或简介也进入 `unresolved`；同一规范化 URL 后续有完整记录时采用该记录，之前缺项记录改记为 `duplicate`，不再作为未解决项。不同页面的缺项仍保留，已有完整记录照常交付。

页数优先级保持首次完整入选的顺序。若完整 B 已占满上限，后来补齐的 A 仍按 `page_limit` 不输出；但 A 的先前缺项已得到补充，不能继续报告资料缺失。私有或无效记录不能补足公开页面的资料。
- `section` 默认为“页面”，实际分组由 Agent 选择。按完整记录被采用的顺序排列页面与首次出现的分组；不按路径猜分组。
- `visibility` 默认 `public`；其他值均排除。`status` 默认 `published`；其他值均排除。默认值只是用户清单的输入约定，不证明网站真的公开。
- 可选 `source` 为资料来源说明，默认 `provided_text`。发现命令记录实际读取时间与 URL；`build` 会转存该说明，但不会把输入中的在线观察当作本次网络检查。
- 中文不自动截断。所有标题、简介压成单行，转义 Markdown 特殊字符与 HTML。超过 160 字符的简介保留全文并提醒 Agent 精简。

## 已实现命令

从技能根目录执行，输出路径按当前任务替换：

```sh
python3 scripts/llms_index.py build --input examples/four-pages.json --max-pages 20 --output output/llms.txt --report output/链接校验报告.json
python3 scripts/llms_index.py discover --site https://example.com/ --sitemap https://example.com/sitemap.xml --max-pages 20 --max-sitemaps 5 --max-candidates 200 --timeout 8 --output output/pages.json --report output/发现报告.json
python3 scripts/llms_index.py discover --site https://example.com/ --urls urls.txt --exclude /members --output output/pages.json --report output/发现报告.json
python3 scripts/llms_index.py validate --input output/llms.txt --site https://example.com/ --report output/本地复核.json
python3 scripts/llms_index.py validate --input output/llms.txt --site https://example.com/ --online --timeout 8 --report output/在线复核.json
```

`--report` 总是必填；`build/discover` 还需 `--output`。输入、输出、报告不能指向同一文件。`--exclude` 可重复，每次给一个路径前缀（如 `/members`），按路径段边界匹配。没有额外的格式、语言、部署或交互选项。

`discover` 的 `--urls` 与 `--sitemap` 二选一；两者均省略时读 `/sitemap.xml`。URL 文件每行一条，允许站内相对路径、空行与 `#` 开头注释。只处理 XML urlset/sitemapindex，不处理 gzip sitemap、浏览器渲染或登录页面。最多解析每个 sitemap 的前 `max-candidates` 个条目；边界截断在报告提醒，不称全站覆盖。

`max-pages` 默认 20，范围 1–200：`build` 的最大输出页数；`discover` 的最大候选页面尝试数，失败也占一次，不无限补抓。`max-sitemaps` 默认 5，范围 1–20；`max-candidates` 默认 200，范围 1–1000。重定向最多 5 跳，每跳先检查范围；每次 HTTP 超时默认 8 秒，允许 1–30 秒；每份响应最多 2 MiB。`validate --online` 最多请求前 200 条有效链接，超过则报错，不会将其余标成已检查。

`discover` 输出 `pages.json` 是已读素材，不是自动写好的文案。网站名与介绍仅在候选中实际读到 `site.url` 对应主页时提取，否则只留下网站 URL。Agent 先整理分组、按事实核对与精简，补上用户已提供的网站名和介绍，再 `build`。如果目标站只有客户端渲染内容，请用户提供页面文本或已有导出内容；已有可读页面继续交付。

## 范围与请求

同源要求协议、hostname、有效端口全部相同；默认端口等价，域名大小写等价，`www` 和其他子域名不自动合并。跨站 301/302 等不会被跟随；换域后要用户明确新的站点范围。过滤已知后台/私有/草稿/登录路径、敏感查询参数和显式非公开状态；对编码后的路径也检查。主 URL 与 `#/`、`#!/` 路由的路径和查询分别检查：`#/admin?tab=users`、`#/guide?visibility=private`、`#!/guide?preview=true` 及编码形式均排除；`--exclude` 也应用于功能路由路径。

不同功能路由保留完整目标，如 `/#/products` 与 `/#/tutorials` 不合并为首页，路由里的 `?lang=zh` 与 `?lang=en` 也保留。普通 `#answer` 页内锚点去掉；主查询与路由查询里的 `utm_*`、gclid、fbclid、msclkid 去掉，其余参数保留原始编码、顺序及同名参数的先后，不对值做 UTF-8 解码重编码，保留 GBK 百分号编码、`+`、`%20`、无等号标志与空值。不把 `/path` 与 `/path/` 擅自合并。离线输入已有标题与简介时可直接交付功能路由；在线发现或校验不执行客户端路由，报告 `hash_route_requires_supplied_content` 并且不请求主页来冒充路由页面，需用户提供实际文本后离线生成。

尚未编码的中文查询按 UTF-8 百分号编码后用于真实请求及输出链接；查询中的原始括号编码为 `%28`/`%29`，避免截断 Markdown 目标。已有百分号字节不双重编码。Markdown 目标里的 HTML 实体按语义解码一次后再执行私有参数过滤与在线请求，例如 `&amp;visibility=private` 按 `&visibility=private` 排除；生成器对 URL 中原本就像实体的字面序列转义，保持实际目标不变。

在线模式使用无 Cookie、无认证、无环境代理的公开 HTTP GET；不调用浏览器、模型或宿主系统。只有 HTML title 或 H1，加 meta description、OG description 或首个正文段落齐全时才纳入发现结果。HTML 无值属性按空值处理，不因 `<input type>` 或 `<meta name="robots" content>` 中断其他页面。未知编码的 sitemap 记 `cannot_decode_sitemap`，继续处理其他 sitemap 和可用页面。HTTP 失败与超时单列；密码表单、meta/X-Robots-Tag noindex 作为排除项（noindex 本身并不代表访问控制）。

重复的 `X-Robots-Tag` 响应字段不互相覆盖；任一字段含 noindex，发现与在线校验均排除该目标。

正文段落支持省略 `</p>`：新段落、块级边界、父块结束及文件结束时保存当前段落，不将后续块或段落粘入首段。sitemap 的条目缺失 `loc`、空 `loc` 或仅空白时记录 `sitemap_loc_required`，包含 sitemap URL 与从 0 起的 `entry_index`；已读正常页面保留，发现结果为未完整通过。

## 报告语义

| 字段 | 含义 |
| --- | --- |
| `valid` | 当前命令是否通过；不代表内容被引擎采纳 |
| `evidence_kind` | `local` 为本地转换/校验，`online` 为真实 HTTP，`simulated` 仅为测试注入 |
| `online_check` | `not_run` 从未发送请求；`online` 有真实 HTTP 尝试，需逐条看结果；`simulated` 为模拟，不能当线上证据 |
| `links` | 生成、发现或校验的逐条记录；离线为 `not_checked`，在线可为 `reachable/http_error/request_failed/excluded/unresolved` |
| `excluded` | 同源、公开状态、去重和数量规则排除；不是请求失败统计 |
| `unresolved` | 生成与发现中的格式错误 URL、缺标题/简介、无法解析或无法读取页面文本等，需要更正或补资料；发现的 URL、sitemap 和重定向目标均适用 |
| `failed_links` | 实际尝试后 HTTP 错误、网络异常、重定向循环或响应超限；离线不得猜测链接坏了 |
| `sitemaps` | 发现命令实际读到的 sitemap 请求记录 |
| `limits/warnings` | 本次上限与截断提示；达到上限不等于全站已发现 |
| `counts` | 各数组的真实长度；`validate` 中失败链接也在 `links` 内，各项不是互斥分母 |
| `output_status` | build 的 `complete/partial/not_written`；部分输出不算完整通过，输出写入失败也标记 `not_written` |

`checked_at`、`http_status`、重定向链只在对应请求发生后记录。失败的实际请求仍可使 `online_check=online`，但不会得到 `reachable`；这不是“全通过”。提供的 `source` 不替代当前检查状态。报告不生成提及、引用、收录或排名分数。

退出码：`0` 当前范围通过；`1` 业务失败或部分可用（报告具体原因，仍可保存可用材料）；`2` 输入/参数/文件错误（stderr 输出错误，不进入交互）。缺整个网站主体时不生成虚构介绍；没有可用页时不写索引，也不删除已有索引，应结合本次报告判断是否有新输出。

输出先写入同目录临时文件，完成后替换目标，再保存成功报告。输出路径是目录、无法写入或替换失败时返回 `2`，旧索引保持原样；报告可写时保存 `valid: false`、`output_status: not_written` 与 `errors[].reason: output_write_failed`，不能将旧文件作为本次交付。若报告也无法保存，stderr 另含 `report_error`。发生参数或文件错误时不保证报告文件存在，必须结合本次退出码与 stderr 判断结果。

本地 `validate` 校验本技能使用的紧凑 Markdown 子集：一个站点 H1、介绍引用块、H2 分组、带标题/绝对 HTTP URL/简介的列表链接与重复/范围检查，不是所有 llms.txt 写法的官方兼容性判定器。事实质量由 Agent 阅读材料判断。

链接列表接受 `-`、`*`、`+` 及数字加 `.`/`)` 的普通标记。遇到尚未处理的行内、引用式或自动链接，记录 `unresolved[].reason: unsupported_markdown_link`，不把文件判为全部通过；包括快捷引用的 `[名称]: URL` 定义及大小写混合的 HTTP(S) 自动链接。列表简介中附带的其他链接也不能静默忽略。

实际 HTML 标签中的 `href` 链接（如 `a`、`area`）记为 `unsupported_html_link`，不请求地址，也不完整通过；标题、介绍与普通行使用同一判断。HTML 实体及奇数反斜杠转义的标签源码是字面文字。`build` 把提供的纯文本 HTML 转义保存，生成后仍可本地 `validate`。
