# creo-mcp 架构审计（0.15.0）

> 审计人：Team Lead（原委派给 code-auditor 的 `task-3`，该队友中途失败、未产出文件，本审计由 Lead 重新取证完成）。
> 取证方式：逐文件读取 + 实机命令验证。**所有行号均为本次实测行号**（工作区为改动后状态）。未逐行读取的部分已明确标注「未深查」。
> 配套阅读：中望参照分析见 [ZW3D_MCP_ANALYSIS.zh-CN.md](ZW3D_MCP_ANALYSIS.zh-CN.md)，规则见 [../TOOL_CONVENTIONS.zh-CN.md](../TOOL_CONVENTIONS.zh-CN.md)。

---

## 1. 现状盘点

### 1.1 模块地图（LOC 由 `Get-Content | Measure` 实测）

| 模块 | LOC | 职责 |
|---|---|---|
| `creo_mcp/server.py` | 1606 | MCP 装配：instructions、7 资源、29 工具、进程/锁/超时、CLI `main()` |
| `creo_mcp/bridge.py` | 2499 | 官方 VB API/COM 调用、连接生命周期、单位与几何测量（**内部逻辑未深查**，仅结构与关键行取证） |
| `creo_mcp/models.py` | 1401 | pydantic 请求/结果模型，`extra="forbid"` |
| `creo_mcp/diagnostics.py` | 705 | 本次新增：`--doctor` 预检 |
| `creo_mcp/catalog.py` | 468 | 本次新增：工具目录 + 契约检查 |
| `creo_mcp/broker.py` | 439 | 单实例 broker（命名管道 + 状态文件 + 自动拉起，`-m creo_mcp.broker`） |
| `creo_mcp/clearance_geometry.py` | 786 | 托盘概念几何规划（不依赖 Creo） |
| `creo_mcp/geometry_dsl.py` | 493 | JSON 几何程序 DSL |
| 其余（cad/step/udf/fit_analysis/preview_render/concept_geometry/observability/screenshot/errors/version） | ~1000 | 校验、导入、渲染、审计日志等 |

包内 Python 合计约 8,099 行（不含本次新增），测试约 2,958 行 → 现共 **143 个测试**（`pytest -q` 全绿）。

### 1.2 进程与调用模型

```text
MCP 客户端 ──stdio/http── server.py（工具层，pydantic 校验、确认令牌、进度中继）
                              │  _call_creo()
                              ▼
                    broker.py（单实例命名管道服务，自动拉起，串行锁）
                              │  在 broker 进程内执行
                              ▼
                    bridge.py ── pfcls COM ──▶ Creo (xtop.exe / pfclscom.exe)
旁路：--persistent-worker（长驻连接，server.py:557 启动）、--operation（一次性，仅 README 手工诊断与测试使用）
```

关键点：`_run_isolated_operation` / `_status_from_isolated_process`（`server.py:747`、`server.py:736`）**实际经 broker**，并非"隔离子进程"；真正的子进程隔离只剩 `_run_isolated_process`（`server.py:322`），而它在生产代码里**没有调用者**（仅 `tests/test_connection_safety.py:193/216/248` 直接调用）。命名与实现已经不符。

### 1.3 工具面（实测 `initialize` / `tools/list` / `resources/list`）

- 29 个工具：`meta` 3（`creo_get_status`、`creo_tool_catalog`、`creo_get_capabilities`）、`read` 14、`modify` 1、`write` 11（`--list` 头部输出）。
- 7 个资源：5 个文档 + 本次新增 `creo://server/status`、`creo://tools/catalog`。**0 个 prompt**，但 `initialize` 仍广播 `prompts` capability（实测响应）。
- `instructions` 811 字符；`annotations` 覆盖 29/29；`outputSchema` 覆盖 26/29（2 个发现型元工具刻意轻量、`creo_capture_screenshot` 返回 `Image`）。
- `tools/list` 载荷 **135,749 字节**（实测）。
- CLI：`--list` / `--list-json` / `--doctor [--probe|--json]` / `--transport {stdio,streamable-http}` / `--host` / `--port` / `--help` / 原有 `--broker*`、`--probe-status`、`--persistent-worker`、`--operation`。

### 1.4 超时与并发（`server.py:109-111`）

`PROBE_TIMEOUT_SECONDS = 30`、`OPERATION_TIMEOUT_SECONDS = 420`、`MCP_CALL_LOCK_TIMEOUT_SECONDS = 5`；`_ISOLATED_CALL_LOCK` 全局串行（`server.py:259`）。README 的客户端示例写 `"toolTimeout": 150`（`README.md:253`）**小于 420**——长操作会被客户端先杀，见缺陷 A-4。

---

## 2. MCP 协议符合度

实测 `initialize` 返回 `protocolVersion "2025-06-18"`，capabilities 为 `tools/resources/prompts（listChanged=false）`，`serverInfo` 名称与版本正确（0.15.0）。

| 项 | 结论 |
|---|---|
| 工具命名/描述/schema | 全部有 title、description、inputSchema、annotations；描述与命名规则现由 `tests/test_tool_conventions.py` 强制 |
| 结构化输出 | 26/29 有 outputSchema；元工具豁免，规则见 `docs/TOOL_CONVENTIONS.zh-CN.md` 第 2 节 |
| annotations 语义 | 三档常量（read/write/model-edit）且现与动词类别交叉校验，一致 |
| resources | URI 全部可读（`creo://server/status` 与 `creo://tools/catalog` 实测返回内容） |
| prompts | 未实现，却广播 capability（P2） |
| 进度 | `_run_isolated_operation_with_progress`（`server.py:793`）用 `asyncio.to_thread` 保持事件循环可用并中继 broker 进度帧，设计正确 |
| 取消 | `_await_with_progress`（`server.py:777`）在 `CancelledError` 时不杀 COM 调用、后台收尾，符合"不能中途杀 COM"的约束 |
| 错误 | 统一 `CreoMcpError`（`errors.py` 仅 7 行，异常类型过于单薄：没有稳定 `code` 字段，见 A-6）；工具级失败走 FastMCP 的 `isError`，未见把内部 COM 细节原样吐给模型的路径（`--doctor` 与日志承担细节） |
| stdout 纯度 | 库函数不打印（实测 `print(` 只出现在 `server.py` 的 CLI 分支）；无 stderr 日志文件轮转（见 A-8） |

---

## 3. 与 ZW3D MCP 的逐项对照（结论版）

| 能力 | ZW3D | creo-mcp 0.15.0 | 判断 |
|---|---|---|---|
| 安装/分发 | npm 启动器（注册表探测、插件下载、DLL 部署、提权指引） | 无启动器；pipx/venv + `--doctor` 自检 | 已补可运维性，**不需要**照抄 DLL 部署 |
| 启动失败 UX | `initialize` 返回 JSON-RPC 错误 + 中文可操作指引 | 服务照常起，工具级报错；`--doctor` 前置诊断 | **仍有差距**（P1，见 A-1） |
| 多版本/多安装 | 版本家族映射 + 全家族部署 | 单一 loadpoint（注册表解析） | Creo 场景暂不需要 |
| 工具枚举自检 | `--list` / `--list-json` | 本次新增，且带契约检查 | 已对齐并更强（含 find_issues） |
| 元工具 | status / catalog / capabilities | 同名三件（`creo_get_status`/`creo_tool_catalog`/`creo_get_capabilities`） | 已对齐 |
| 资源 | status / catalog / session history | 5 文档 + status + catalog；**无 session history** | 差距：`creo_get_operation_log` 工具存在，但没有对应 resource（P2） |
| prompts | 2 个（建模助手、失败调试） | 0 个 | 差距（P2） |
| 传输 | stdio + streamable-http | 本次新增两者 | 已对齐 |
| 结构化输出 | 0/59 | 26/29 | **creo 明显更好，勿退化** |
| 描述卫生 | 5 处引用不存在工具 | 0 处（测试强制） | 已对齐 |
| 日志 | 按天文件 + stderr 纪律 | `observability.py` JSONL 审计，无按天运行日志 | 差距（A-8） |
| 授权/计量 | OAuth + 网关 + DLL 内 license | 无 | 自用无需；分发时另议 |
| 测试 | 不可见 | 143 个，可复现 | creo 优势 |

---

## 4. 缺陷与风险清单

> 严重度：P0 发布前必须处理；P1 下一迭代；P2 择机。**A-2/A-3/A-5/A-9/A-10/A-12 本次已修**，其余为遗留。

| ID | 级别 | 证据 | 影响 | 处置 |
|---|---|---|---|---|
| A-1 | **P1** | `server.py:1564`（`main()` 直接进入 transport，不校验环境）；对照 ZW3D `index.js` `buildBlockMessage`/`scheduleMcpErrorExit` | Creo 未运行/未注册 VB API 时，客户端显示"已连接"，直到第一次工具调用才失败，用户看不到根因 | 加"启动预检"策略：默认 warn 并继续（保持可离线规划），提供 `CREO_MCP_REQUIRE_CONNECTION=1` 时在 `initialize` 前失败并给出可操作中文提示；或让宿主安装脚本先跑 `--doctor` |
| A-2 | **P1（已修）** | 原 `models.py` `CreatePartRequest`（现 `models.py:138`）缺 `confirmation`；`server.py` 描述未写令牌 | 违反 ARCHITECTURE 自己的"会话变更工具必须显式确认"约束，模型可无确认改会话 | 已加 `confirmation: Literal["create_empty_part"]` + 描述 + `tests/test_tool_conventions.py` 强制（并检查描述里逐字出现该令牌） |
| A-3 | **P1（已修）** | `README.md` 旧版用 `python -m unittest discover -s tests`；实测 `unittest discover` 只跑 **96** 个，`pytest` 跑 **143** | 按文档验证的人会以为 45 个测试（含全部新契约测试）不存在 | 已改为 `pip install -e ".[dev]"` + `pytest -q`，并在 README 说明 unittest 会静默漏 |
| A-4 | P1 | `README.md:253` `toolTimeout: 150` vs `server.py:110` `OPERATION_TIMEOUT_SECONDS = 420` | 长导入/托盘创建被客户端先杀，服务端仍在跑 → 状态不一致与"未知结果"写入风险 | README 示例改为 ≥420 或说明两者的关系与取值原则 |
| A-5 | **P1（已修）** | `server.py:267` 原以 `parents[1]/creo_mcp_server.py` 启动 persistent worker | wheel/非仓库目录安装时该文件不存在 → worker 起不来 | 已加 `_worker_command()`（`server.py:271`）回落到 `-m creo_mcp.server`，并补 `if __name__ == "__main__"` 与两条回归测试 |
| A-6 | P1 | `creo_mcp/errors.py` 全文 7 行，仅一个 `CreoMcpError(Exception)`；`server.py:743/757` 直接把 broker 文本塞进异常 | 模型无法区分"可重试 / 需用户干预 / 契约错误"；重试策略只能靠猜 | 给错误加稳定 `code`（如 `creo_busy`、`creo_not_running`、`invalid_reference`、`confirmation_required`）并让 `docs/TOOL_CONVENTIONS.zh-CN.md` 第 8 节落地 |
| A-7 | P1 | `bridge.py` 47 处 `except Exception`（实测计数），`server.py` 8 处；`server.py:773` 为 `except Exception: pass`（ruff S110） | 进度中继失败静默；COM 错误可能被降级成"成功但无几何" | 至少把 `pass` 改成记日志；对写路径的兜底 except 收敛为具体异常类型 |
| A-8 | P1 | `observability.py` 只有操作审计 JSONL；无按天运行日志、无 stderr 纪律说明 | 现场排障（尤其 A-1 场景）缺证据链；对照 ZW3D `logs/zw3d-mcp-YYYY.MM.DD.log` | 加 `%LOCALAPPDATA%\CreoMCP\logs\creo-mcp-YYYY.MM.DD.log` 运行日志 + 明确 stdout 只属于协议 |
| A-9 | **P1（已修）** | 安装元数据 `pip show creo-mcp` = **0.10.1**，而 `pyproject.toml:3` = 0.15.0 | 版本漂移导致排障与文档对不上；`--list` 头部显示正确版本但安装记录说谎 | 本次已 `pip install -e . --no-deps` 刷新为 0.15.0；建议把"安装版本 == SERVER_VERSION"写成一条测试或 doctor 检查项 |
| A-10 | **P2（已修）** | `_run_isolated_process`（`server.py:322`）无生产调用者，仅测试引用；`_run_isolated_operation` 名字与实际 broker 路径不符 | 读者误以为每次操作都是独立进程，安全边界判断错误 | 已在文档层澄清；建议下一迭代改名 `_run_broker_operation` 并删死代码或补 `--operation` 路径的用途说明 |
| A-11 | P2 | 无任何 CI（`.github/`、`tox.ini`、`noxfile.py` 均不存在，实测 glob 为空）；`ruff check --isolated creo_mcp/server.py creo_mcp/bridge.py` 报 **48** 处（38 blind-except、7 try/except-pass、import 排序、deprecated typing import） | 143 个测试从不被自动执行；lint 结果依赖个人机器 | 建最小 CI：`pytest -q` + `ruff check` + `creo-mcp --list-json` 快照 |
| A-12 | **P2（已修）** | `pyproject.toml` 原先无 dev 依赖组，`pytest`/`ruff` 长期未声明却实际使用 | 新环境按 README 装完跑不了测试 | 已加 `[project.optional-dependencies] dev = ["pytest>=8","ruff>=0.6"]` |
| A-13 | P2 | `bridge.py:12 import winreg`、`pyproject.toml:9` 标记 `sys_platform=='win32'`，但 `server.py:23` 无条件 `from .bridge import ...` | 非 Windows 上 `import creo_mcp.server` 直接失败，与"requires-python >=3.11 跨平台"暗示不符 | 要么显式声明 Windows-only，要么让离线规划工具（`plan_*`、`inspect_step_*`）在未装 pywin32 时可用 |
| A-14 | P2 | `initialize` 广播 `prompts` capability 但 `prompts/list` 为空（实测）；对照 ZW3D 有 `zw3d_debug_failed_command` | 能力虚报（轻微）；少了一个低成本高收益的调试入口 | 加 1 个"诊断失败调用"prompt，或接受 FastMCP 的默认广播并记录说明 |
| A-15 | P2 | 根目录 `_asm00_program.json`（已 `git ls-files` 确认被跟踪）；`__pycache__` 中存在 `reference_analysis`、`silhouette_geometry`、`test_concept_*`、`test_fit_analysis` 等**无对应源文件**的陈旧 pyc | 仓库里留有实验残渣，读者误判存在模块 | 把 `_asm00_program.json` 移到 `live-validation/` 或 `.gitignore`；pycache 属本机清理项（`.gitignore` 已覆盖目录） |
| A-16 | P2 | `tools/list` 135,749 字节，其中 outputSchema 76,900（最大单项 `creo_create_product_clearance_tray` 7,991） | 每轮对话固定成本偏高 | 按 `TOOL_CONVENTIONS.zh-CN.md` 第 7 节：对"一次性大报告"型输出改为精简输出 + 二次查询；已用预算测试锁住上限 150,000 |
| A-17 | P2 | `broker.status()` 只报运行与否；无 trace id 贯穿 server→broker→worker | 一次调用失败时无法把三段日志串起来 | 引入 `request_id`（uuid，已在 `_execute_operation` 用到 operation 级）并写入日志与错误体 |

| A-18 | **P1（已修）** | 实测 `creo-mcp.exe --operation inspect_model_structure --payload '{}'` → `exit 1`，错误来自 `bridge.py:910`（"Creo is busy…"）；此时 broker(pid 5316) 正在持有 VB API 连接 | README 推荐的单机诊断路径（`--probe-status`、`--operation`）在 broker 运行时必然失败，运维照做会误判为 Creo 故障 | 已实现：`_run_operation` 在 `broker.ping()` 存活时改走 broker（与工具面同路径），无 broker 时才直连；修复后实测 `ok=true, via=broker`，回归测试见 `tests/test_cli_dispatch.py` |
| A-19 | **P2（已修）** | `broker.runtime_dir()` 原先只读 `LOCALAPPDATA`；实测跑完 `pytest -q` 后 `%LOCALAPPDATA%\CreoMCP\logs\broker.log` 出现 `creo-mcp-test-<hex>` 的 starting/connected/stopped 记录 | 测试污染生产运行日志，事后排障时真假记录混在一起 | 已实现：`CREO_MCP_RUNTIME_DIR` 覆盖 + `tests/conftest.py` 会话级隔离；实测 pytest 前后生产日志字节数不变（32299 → 32299） |
| A-20 | **P3（语义未文档化，非缺陷）** | 实机创建 `ZHENGGUAN_TRAY_DRAFT_R01` 后：结果 `"activated": true`，但 `creo_get_status` 返回 `active_model: null`；`inspect_model_structure {}`（活动模型路径）报 `The requested Creo model is not loaded in the session.`，而 `{"model_name":"ZHENGGUAN_TRAY_DRAFT_R01","model_kind":"part"}` 正常读到全部结构。根因见 `bridge.py:756-762`：`_activate_and_repaint_model` **只走 `pfcModel.Display`**，并明确禁用 `CreateModelWindow` 与 `Window.Activate`（两者实测会破坏 Creo 13.4 图形状态：ORDER_DEBUG + 后续访问越界）。因此 7 处 `activated=request.activate`（1035/1102/1290/1390/1433/1474/1564）表达的是"已按请求走显示路径"，不是"Creo 已把它设为活动窗口" | 客户端/工程师会误读 `activated`，以为窗口已前置；排障时误判模型丢失 | 待办：在 `activated` 字段与 `--help` 文档写清语义；如需可回读状态，**新增** `active_model` 字段而不是改写 `activated`。**禁止**为了填满 `activated` 而重新引入 `Window.Activate`/`CreateModelWindow`（会崩 Creo） |
| A-21 | **非缺陷（已澄清）** | `ProductClearanceTrayPlan.static_outside_volume_mm3` 与 `insertion_sweep_outside_volume_mm3` 在方案阶段返回 0.0，曾被误读为"体积未回填"。实为**违规体积度量**：`_verify_static_containment()` 返回"产品落在型腔外的体积"，超容差直接抛错（`clearance_geometry.py:500-509`） | 字段名极易被当成外形体积；本轮已收回"顺手改成预估体积"的错误提议 | 已在 `models.py:376-389` 给两个字段补 description，说明 0.0 = 通过；数值语义未改动 |

### 4.1 明确"不要照抄中望"的条目

1. 59 个扁平工具 + 34 KB 描述（我们 29 个 / 7.6 KB）；
2. 全部结果字符串化 JSON、无 outputSchema（我们是客户端可校验的结构化输出）；
3. `readOnlyHint` 乱标（我们由测试与动词白名单交叉校验）；
4. 描述引用不存在的工具（我们有 `find_issues` + `test_no_tool_description_references_a_missing_tool`）；
5. 把 `ipc_error` 这类内部细节当唯一错误信息回给模型；
6. 单工具 20 个入参（`zw3d_pattern`）——我们的"单 request 对象"更稳。

---

## 5. 优先级建议

- **P0**：无（本次未发现会导致静默数据损坏的缺陷；写路径的覆盖保护、确认令牌与不可自动重试策略均有效）。
- **P1（下一迭代）**：A-1 启动预检策略 → A-6 结构化错误码 → A-8 运行日志 → A-4 客户端超时对齐 → A-7 收敛 blind except →（可选）A-13 跨平台导入边界。
- **P2**：A-11 最小 CI、A-14 prompts、A-16 载荷瘦身、A-15 仓库清洁、A-17 trace id。
- **保持不动**：结构化输出覆盖、annotations 三档、确认令牌、进度/取消语义、96→143 测试网。这些是我们已经优于参照对象的地方。

---

## 6. 验证方法（可复现）

```powershell
cd <repo>
.\.venv\Scripts\python.exe -m pytest -q                     # 143 passed
.\.venv\Scripts\python.exe -m unittest discover -s tests    # 仅 96（A-3 证据）
.\.venv\Scripts\python.exe -m ruff check --isolated creo_mcp\server.py creo_mcp\bridge.py --statistics  # 48（A-11 证据）
.\.venv\Scripts\python.exe -m pip show creo-mcp             # Version: 0.15.0（A-9 修复后）
.\.venv\Scripts\creo-mcp.exe --list                          # 29 tools: meta 3, read 14, modify 1, write 11
.\.venv\Scripts\creo-mcp.exe --list-json                     # 合法 JSON，count=29
.\.venv\Scripts\creo-mcp.exe --doctor                        # ok=9 skip=1，exit 0
.\.venv\Scripts\creo-mcp.exe --doctor --probe                # 队友实测 10/10 ok，connected=True
```

实机会话验证（stdio JSON-RPC，12/12 通过）：`initialize` → `tools/list`(29, 135,749 B) → `resources/list`(7) → `tools/call creo_tool_catalog` → `tools/call creo_get_capabilities` → `resources/read creo://server/status` → `resources/read creo://tools/catalog` → **真实 Creo 会话**：`creo_get_status` 返回 `connected=true, loadpoint=<Creo 安装目录>, active_model=zhengguan_box_r01.prt`；`creo_get_active_model` 返回 `ZHENGGUAN_BOX_R01`。

未深查项：`bridge.py` 的 COM 细节实现（连接重试、单位换算路径、UDF 放置的具体 API 序列）本轮未逐行读取；`docs/LIVE_VALIDATION.zh-CN.md` 的既往记录未逐条复核。相关结论只使用可 grep/实测的证据。
