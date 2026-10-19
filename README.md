# CNKI RefWorks Exporter

使用 Python 和 Playwright，批量读取中国知网官方 **RefWorks 标签格式**题录，自动校验、去重并汇总为 TXT。

本项目由原 V2 脚本整理而来，主入口改名为 **`cnki_refworks_exporter.py`**。`cnki_refworks_v2.py` 保留为兼容入口，原命令、配置和 `output_v2` 进度可以继续使用。

## 工作方式

用户在浏览器中登录并设置检索，程序随后执行：

```text
勾选本页 → 导出与分析 → 导出文献 → Refworks
→ 读取官方题录 → 核对篇数与题名 → 保存批次
→ 返回结果页并取消勾选 → 下一页
```

程序直接读取导出页面的标签文本，支持新标签页、当前页及 iframe，无需手工复制到 Windows 剪贴板。每批保存原始标签文本，汇总文件另行规范化。

- 按期刊或“期刊 × 年份”生成检索任务。
- 本页全选或分组导出，批次大小可配置。
- 核对勾选与实际导出的篇数、题名及重复次数，发现截断或错页就停止当前任务。
- SQLite 保存进度，重新运行时跳过已验证批次。
- 基于 DOI、CNKI 标识及题名等去重，保留来源和字段冲突。
- 保留没有明确年份的网络首发题录，并与严格研究子集区分。
- 支持批量导入已有的 RefWorks TXT。
- 自动菜单操作失败时，可手动打开 RefWorks 页面后继续读取。

需要自行具备相应的知网访问条件。登录、验证码由用户在浏览器中完成；本项目不包含验证码求解或权限绕过，也不是无人值守的自动检索服务。

## 安装

需要 Python 3.10 或更高版本。Windows 已有实际使用反馈；其他系统请配置 Playwright 并验证。

下载仓库 ZIP 并解压，或克隆：

```powershell
git clone https://github.com/Huberyky/cnki-refworks-exporter.git
cd cnki-refworks-exporter
```

在 PowerShell 中逐行执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

无需激活虚拟环境或修改 PowerShell 执行策略。macOS/Linux 将解释器替换为 `.venv/bin/python`；Linux 系统依赖参见 [Playwright 安装文档](https://playwright.dev/python/docs/intro)。

`plan`、`import`、`export` 只需 Python 标准库；`crawl`、`inspect` 需要浏览器依赖。

## 第一次运行

生成任务表，核对范围：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py plan
```

`plan` 只生成 `output_v2/search_plan.csv`，不会访问知网或采集题录。

启动采集：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl
```

1. 在独立浏览器中登录、完成机构认证或验证码，然后回终端按回车。
2. 按终端任务设置数据库类型、主题、精确期刊来源和年份，进入结果第一页。
3. 清除此前跨页保留的已选文献，核对条件后回终端按回车。
4. 程序自动勾选、导出、核对、保存并翻页。避免同时手动操作正在导出的页面。
5. 未找到可用下一页时，确认末页再输入 `end`；仍有下一页则手动翻页后按回车。
6. 终端提示下一个任务时，设置下一种期刊继续。

其他输入：`empty` 登记人工核对后的零结果，`skip` 跳过任务，`quit` 暂停或退出。验证页、加载失败页不能登记为零结果。

建议首次只处理一页：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl --max-pages 1
```

进入下一任务提示时输入 `quit`，检查结果后正式运行。达到页数限制不代表采集完成。

## 检索配置

编辑 `config_v2.json` 的 `start_year`、`end_year`、`keywords`、`journals`。示例预设 15 种民族研究相关期刊、主题“民族 AND 文化”、1978—2026 年，使用者应按自己的研究修改。

```json
{
  "start_year": 1978,
  "end_year": 2026,
  "keywords": ["民族", "文化"],
  "journals": [
    {"name": "民族研究", "aliases": []}
  ]
}
```

以上为节选，保留完整文件其他参数。关键词用于生成 AND 条件，实际检索仍需用户在页面设置；导出 TXT 不能证明此前检索条件正确。

`aliases` 可填写已核实的曾用刊名或名称变体，页面检索也要包含相应名称。避免混入不同期刊或同校自然科学版。年份包含两个端点，未出版或未收录的文献无法取得。

逐年拆分或指定其他配置：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py plan --split year --out output_year
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl --split year --out output_year
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl --config my_config.json --out output_custom
```

`plan` 和 `crawl` 应使用一致的配置、拆分方式和输出目录。

## 输出文件

默认目录为 `output_v2`，优先查看 `all_refworks.txt`。

| 文件 | 内容 |
| --- | --- |
| `all_refworks.txt` | 全部成功导出并去重的题录，不按期刊或年份排除 |
| `raw_refworks/*.txt` | 已验证批次的标签文本，保留标签顺序、原始 SR，去掉工具栏文字 |
| `cnki_refworks.txt` | 按配置期刊和年份筛选的研究子集 |
| `needs_review_refworks.txt` | 缺少年份等需要核对的记录 |
| `metadata_audit.csv` | 纳入、排除、待核对和缺失字段原因 |
| `records.json` | 标签字段、来源、时间和字段冲突 |
| `batch_log.json` | 批次题名、篇数、状态及文件位置 |
| `task_log.json` / `summary.json` | 任务状态和当前统计 |
| `progress.sqlite3` | 续跑数据库 |
| `failed_batches/*.txt` | 未通过验证的候选内容，不纳入汇总 |

完整汇总和研究子集数量可能不同，主输出条数不应简单等同于知网检索总数。

格式为 **CNKI RefWorks**，不是 RIS 或排版后的 GB/T 7714 引用字符串，支持 `RT / SR / A1 / AD / T1 / JF / YR / IS / vo / OP / K1 / AB / SN / CN / LA / DS / LK / DO` 等标签。机构、摘要、刊号以官方实际导出为准，缺失事实字段不编造。

汇总会规范化关键词分号、纯数字期号和 DOI 链接。每条 `SR 1` 是电子来源类型，不是篇目序号。原批次保存在 `raw_refworks`，便于核对。

## 分批和续跑

默认批次大小 50 是程序上限，并非知网官方上限。一页不足 50 条时导出本页，超过时分组。按实际站点限制减小批次：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl --batch-size 20
```

每批保存并提交进度后再继续。中断后重新进入任务第一页，已验证批次会跳过；不是恢复到中断时的页码。不要删除续跑所需数据库和原始批次。

已确认结束的任务默认跳过。重新扫描时从 `search_plan.csv` 取得任务 ID：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py crawl --task-id 实际task_id
```

校验使用篇数和题名多重集合，能拦截截断、错页和部分跨页残留，不能代替对同名文章的作者、年份、DOI 复核，也不证明数据库已显示全部结果。研究者仍需记录检索条件、日期、结果总数。

## 导入已有 TXT

把知网 RefWorks TXT 放入 `imports`，执行：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py import --input imports
.\.venv\Scripts\python.exe cnki_refworks_exporter.py import --input batch01.txt batch02.txt
.\.venv\Scripts\python.exe cnki_refworks_exporter.py export
```

目录模式只读第一层 TXT，支持 UTF-8、带 BOM 的 UTF-8/UTF-16、GB18030。官方字段更新旧值时保留来源和冲突。

如研究要求本地题名、关键词、摘要同时包含全部词，可加 `--strict-local-keywords`。该选项改变筛选口径，不等于数据库主题检索。

## 页面适配和故障排查

自动菜单操作失败时，按提示在脚本浏览器中手动打开 RefWorks 内容，保持页面不动，回终端按回车重读，不要提前翻页。

检查页面控件：

```powershell
.\.venv\Scripts\python.exe cnki_refworks_exporter.py inspect
```

诊断保存控件 JSON 和截图，不读取 Cookie 或输入框值。按页面调整配置：

| 配置 | 用途 |
| --- | --- |
| `ui.result_rows` / `ui.title_link` | 结果行及题名 |
| `ui.next_page` | 唯一的下一页控件 |
| `export_ui.row_checkbox` / `select_all` | 行复选框及表头全选框 |
| `export_ui.clear_selection` | 可选，清除跨页已选控件 |
| `export_menu` / `export_submenu` / `refworks_option` | 可选的导出菜单选择器，默认按文字定位 |
| `content_selector` / `close_export` | 可选的内容区域及关闭按钮 |
| `manual_fallback` | 自动失败后是否允许人工打开继续 |

后四项也在 `export_ui` 内。选择器需对应当前页面，无法保证所有版本通用。

| 现象 | 处理 |
| --- | --- |
| 导出多于勾选 | 清除跨页旧选择重试 |
| 导出少于勾选 | 检查加载和限制，减小批次 |
| 找不到结果行、菜单或复选框 | 确认页面，再用 inspect 校准 |
| 同一页反复出现 | 检查下一页控件，不当作完成 |
| 浏览器 profile 被占用 | 关闭使用相同 profile 的另一个爬虫浏览器 |
| 验证反复出现 | 人工处理，延长 delay_seconds，或手动导出后导入 |

## 文件与测试

```text
cnki_refworks_exporter.py    主入口
cnki_refworks_v2.py          兼容旧命令
cnki_crawler.py              共享解析、格式化和存储工具
config_v2.json              导出配置和示例检索范围
config.json                 共享模块默认配置与测试配置
config_history.json         可选曾用刊名配置
requirements.txt            浏览器依赖
examples/                   虚构题录示例
tests/                      离线和本地浏览器测试
```

测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用虚构题录，浏览器请求被本地拦截，不连接知网。覆盖解析、范围筛选、去重溯源、批次校验、新标签页、iframe、导航恢复、翻页、分批和续跑。用户已反馈批量流程可用；页面更新后应先小批次验证。

仓库只发布代码、配置、说明和虚构测试数据。浏览器 profile、真实题录、数据库、导入文件、环境变量和日志列入 `.gitignore`。
