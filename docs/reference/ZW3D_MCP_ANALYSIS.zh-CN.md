# 中望3D（ZW3D）MCP 实现剖析与借鉴建议

> 分析对象：`<external-archive>\2026-10-23-16-45-04\zw3d\`（连接器归档 + 部署说明 + 宏语言手册）
> 对照对象：本仓库 `<repo>`（Creo MCP，0.14.0，27 个工具，96 个测试）
> 所有数字都来自本地实测，验证方法见文末「附录 A：证据与复现」。

---

## 0. 结论摘要

1. **它是一套"商业分发级"的 CAD MCP**，不是开源源码。真正的实现被编译进 `ZW3D_MCP.exe`（PyInstaller 打包的 Python 3.12 + FastMCP）和 `ZW3D_MCP.dll`（C++ 的中望 APILib 插件），外部能看到的只有 15 KB 的 Node 启动器（`zw3d-mcp@1.0.2`）。
2. **最值得抄的是它的"外壳"**：启动器式安装/部署、启动前预检、失败时用 MCP `initialize` 错误回一条能照做的中文指引、`--list` 自检、按天滚动日志。这些是中望 MCP 工程成熟度的主要来源。
3. **最不该抄的是它的"工具面"**：59 个扁平工具、35 KB 工具描述、没有 outputSchema、`tools/list` 一次吐 ~95 KB；并且有 5 处工具描述引用了**根本不存在的工具**（文档漂移）。
4. **你的 creo-mcp 并不弱**：协议规范度（26/27 有 outputSchema、27/27 有语义正确的 annotations、5 个文档资源、811 字符的 instructions、96 个测试）明显优于中望；弱的是**分发与自检外壳**（没有 `--list`、没有 `--doctor`、只有 stdio）。
5. **一个反直觉的实测结论**：你的 `tools/list` 载荷（~127 KB）其实比中望（~95 KB）更大——因为结构化输出的 JSON Schema（76.9 KB）比中望的全部工具描述（35 KB）还贵。所以"结构化的收益"要用"选择性结构化"来兑现，不能全量铺开。

---

## 1. 材料与方法

归档目录里只有 5 类东西（`Get-ChildItem -Recurse`）：

| 文件 | 大小 | 是什么 |
|---|---|---|
| `connector\zw3d-mcp-1.0.2.tgz` | 15 KB | npm 包 `zw3d-mcp`，只含 `index.js` + LICENSE |
| `connector\zw3d-mcp-plugin-3200.0.5.tgz` | 19 MB | npm 包 `zw3d-mcp-plugin`，含 `ZW3D_MCP.exe`(19 MB) / `ZW3D_MCP.dll`(1.1 MB) / `.env` |
| `connector\已部署\` | — | 解压后的同一套二进制 + `.env` |
| `connector\连接器部署说明.md` | 3.5 KB | 中望官网给第三方（如 WorkBuddy）的连接器部署说明 |
| `macro.txt` | 110 KB | VX/ZW3D 宏语言手册（`[Print,"..."]` 那种命令脚本）——实现工具的"原料" |

关键点：**这不是源码，是发行物**。所以下面的结论分成两类：可以从发行物直接读出来的（启动器 JS、`.env`、`--help`/`--list` 输出、MCP 协议响应、二进制字符串），以及必须靠推断的（exe/dll 内部实现）。后者我会明确标注「推断」。

---

## 2. 架构全貌

```text
┌────────────────┐   MCP (stdio)   ┌──────────────────────────┐
│ MCP 客户端      │ ◄─────────────► │ zw3d-mcp (Node 启动器)     │
│ (WorkBuddy /   │                 │  index.js 1044 行          │
│  Claude / Codex)│                │  · 查注册表找 ZW3D 安装      │
└────────────────┘                 │  · 按版本取/下载插件包       │
                                   │  · 部署 DLL 到 apilibs\     │
                                   │  · tasklist 查进程          │
                                   │  · 失败 → 用 JSON-RPC 错误   │
                                   │    回 initialize（中文可操作）│
                                   └────────────┬─────────────┘
                                                │ spawn(stdio: "inherit")
                                                ▼
                                   ┌──────────────────────────┐
                                   │ ZW3D_MCP.exe              │
                                   │ Python 3.12 + FastMCP     │
                                   │ · 59 个工具（注册表分发）    │
                                   │ · tools/ 下按域拆模块       │
                                   │ · OAuth 登录 + 网关计量     │
                                   └────────────┬─────────────┘
                                                │ 共享内存（ShareMemoryWriter）
                                                │ + 命名事件（Failed to open event）
                                                ▼
                                   ┌──────────────────────────┐
                                   │ ZW3D_MCP.dll (C++ APILib) │
                                   │  · 常驻 ZW3D 进程内         │
                                   │  · 把 JSON 命令翻译成       │
                                   │    cvxPart* 系列 API 调用   │
                                   │  · License 校验 + 用量上报   │
                                   └────────────┬─────────────┘
                                                ▼
                                       中望3D 内核 / 当前会话
```

四个分层各自解决一个不同的问题：

| 层 | 解决什么 | 中望的做法 |
|---|---|---|
| 分发层（Node） | "用户机器上装没装 CAD、插件在不在、会不会白连" | 注册表探测 + 版本家族映射 + npm 缓存 + 哈希比对的 DLL 部署 + 进程检查 + 可操作错误 |
| 协议层（Python） | "LLM 能用什么、怎么调" | FastMCP + 59 工具 + 3 资源 + 2 prompt + 元工具 |
| 桥接层（共享内存） | "跨进程怎么把命令塞进 CAD 主线程" | 一组"每会话固定名"的事件 + 共享内存 JSON 交换 |
| 业务层（C++） | "怎么真正建出几何" | 把工具语义映射到 `cvxPartSkNew` / `cvxPartExtrude` 等 APILib 调用 |

**这个分层本身是正解**，而且和你的 creo-mcp 是同一个思路：你的 `server.py`（协议层）→ `broker.py`/隔离子进程（桥接层）→ `bridge.py`（VB API/COM，业务层）。差别在于**中望多了一层"安装器/启动器"，你少了一层；你的桥接更"进程化"，中望更"内存化"**。

---

## 3. 启动与分发层：它最值得学的地方

`index.js` 的 7 步主线（代码里自己写在头部注释，1–1044 行）：

1. **探测所有安装**：扫 `HKLM/HKCU\SOFTWARE[\WOW6432Node]\ZWSOFT\ZW3D\{version}\{product}\InstallLocation`，并用 `ProductVersion`（如 `32.01`）而不是目录名来定版本（`parseVersion` → `3200`）。`ZW3D_API_DIR` 环境变量可覆盖（绿色版用户的救命变量），目录未登记时按目录名兜底推断。
2. **选版本**：`ZW3D_MCP_PREFERRED_VERSION` 优先，否则取最高版本。
3. **按"版本家族"归组**：所有 `32.xx` 共用 `zw3d-mcp-plugin@3200` 这一份二进制；一个家族**只下载一次**，然后部署到该家族**每一个**安装目录的 `apilibs\`。
4. **缓存与自更新**：`%LOCALAPPDATA%\ZWSOFT\ZW3D_MCP\plugins\v3200\`，用 `npm view` 比对缓存版本，过期就重下；离线则退回缓存（`warn("Cannot check latest plugin version (offline?)")`）。
5. **部署 DLL + .env**：先比大小再比 SHA-1，**内容一致就完全不写盘**（这样非管理员再次连接不会去碰 `Program Files` 而报 EPERM）；失败时区分 `EPERM/EACCES`（没提权）、`EBUSY`（DLL 被运行中的 ZW3D 占用），并生成**可直接粘贴的 `Copy-Item` 命令**。
6. **启动前拦住"连上了但一定全坏"的状态**：`buildBlockMessage()` 判断「DLL 部署失败」或「刚部署完但 ZW3D 已经在跑（插件只在启动瞬间加载）」→ 直接拦。
7. **拦住的方式很讲究**：不是退出码了事，而是 `scheduleMcpErrorExit()` —— 继续读 stdin，等客户端的 `initialize` 请求，然后用 `{"jsonrpc":"2.0","id":N,"error":{"code":-32000,"message":"…中文可操作指引…"}}` 回过去再退出（5 秒兜底）。这样宿主 UI 显示的是"权限不够，请用管理员身份运行 WorkBuddy，或执行以下命令"，而不是含糊的 "Connection closed"。

另外两个细节值得记：

- **stdout 纯度纪律**：头部注释大写强调 `stdout is the MCP protocol channel. ALL diagnostic output MUST go to stderr.` 日志同时写 `%LOCALAPPDATA%\ZWSOFT\ZW3D_MCP\logs\zw3d-mcp-YYYY.MM.DD.log`（按天分文件，带 `+08:00` 时区）。
- **退出码透传 + 优雅降级开关**：`child.on("exit", code => process.exit(code ?? 0))`；`ZW3D_MCP_ALLOW_DEGRADED=1` 可跳过拦截（默认不建议开）。

一句话评价：**这一层是"把一个会失败的桌面 CAD 变成可运维服务"的全部功夫**，也是中望 MCP 真正比多数开源 CAD MCP 完善的地方。

---

## 4. 进程与 IPC：Python ↔ 共享内存 ↔ C++ 插件

从二进制字符串可以确认的：

- `ZW3D_MCP.exe` 是 PyInstaller 单文件包，内含 `mcp.server.fastmcp`、`httpx`、`anyio`、`pydantic` 等，模块清单里能直接看到它的包结构：
  `ZW3D_MCP.mcp_server` / `.mcp_oauth_protocol` / `.mcp_auth_config_sync` / `.ipc` / `.ipc.shared_memory` / `.tools.base_tool` / `.tools.registry` / `.tools.command_runner` / `.tools.zw3d_commands.{appearance,capabilities,datum,delete_entity,edge_ops,feature_draft,feature_edit,feature_mirror,feature_ops,feature_rib,feature_shell,hole,inquiry,part_ops,profile_utils,sheetmetal,sketch,sketch_constraint,sketch_dimension,sketch_extrude,sketch_revolve,validators,wireframe}`
- `ZW3D_MCP.dll` 里的关键字符串：`ShareMemoryWriter: error writing JSON to shared memory`、`ApiCommand::call failed: %s return code=%d`、`ZW_API_*` 一整套中望错误码、`ZW3D MCP auth/license failed:`。
- 工具调用失败时实测返回的正文：`{"function-status":"no data returned from ZW3D","error":...,"ipc_error":"Failed to open event. Error: 2"}` → 确认是"共享内存 + 命名事件"的信号式 IPC（`--list` 与元工具不触发 IPC，所以它们能离线工作）。

**设计取舍**：

| | 中望（常驻 DLL + 共享内存） | creo-mcp（隔离子进程 + COM） |
|---|---|---|
| 延迟 | 低（无进程启动开销） | 每次操作起子进程，冷启动几十~几百 ms |
| 稳定性 | DLL 崩了会带崩 CAD 或反之；协议一变就得同步升 dll+exe | 子进程崩了不影响 MCP 主进程；超时可杀 |
| 线程模型 | 必须把调用弹回 CAD 主线程（事件唤醒） | COM 自动化天然在独立进程里做 |
| 部署 | 要往 `Program Files` 写 DLL（所以才有提权和 EPERM 问题） | 纯用户态 Python 包，无需写受保护目录 |
| 版本耦合 | dll/exe 与 CAD 版本强绑定（3200 家族） | 相对松（VB API/COM 接口稳定） |

**结论**：这两个方案没有绝对优劣，取决于 CAD 提供的 API 形态。中望的 APILib 是"进程内 C++ 头文件 + 导出函数"，只能进 CAD 进程，所以共享内存是必然选择；Creo 的 VB API 本来就是 COM 自动化，隔离子进程反而是更稳的选择。**你不需要抄 IPC 这一层。**

---

## 5. 工具面：59 个工具是怎么组织的

### 5.1 实测数字

| 指标 | 中望 ZW3D MCP | 你的 creo-mcp |
|---|---|---|
| 工具总数 | **59**（57 建模 + 2 元工具） | **27** |
| 命名法 | `zw3d_<域>_<动作>`，查询用 `inq_` 前缀（11 个） | `creo_<动作>_<对象>` |
| 描述总字节 | 35,058 B | 7,678 B |
| 单条描述 | 中位 402 字符，最长 2,825 字符 | 中位 270，最长 809 |
| 入参个数 | 中位 3，最多 20（`zw3d_pattern`） | 中位 1（都是单个 request 对象） |
| annotations | 59/59 **有**，但语义不一致（见 5.4） | 27/27 有，且分 read / write / model-edit 三档 |
| outputSchema | **0/59** | 26/27 |
| `tools/list` 载荷 | ~95 KB | ~127 KB（outputSchema 占 76.9 KB） |

### 5.2 命名法的真实规则（有规律，但没贯彻）

- 域前缀：`appearance_` / `datum_` / `feature_*` / `sketch_*` / `smd_*`（钣金 sheet metal）/ `file_*` / `inq_*` / `tool_*`。
- 动作后缀：`get / list / search / create / add / apply / set / modify / delete / save / open / close / exit / redefine / chamfer / fillet`。
- 查询类多了一个 `inq_` 前缀（inquiry），例如 `zw3d_inq_shape_faces`。
- **不一致的地方**：纯查询的 `zw3d_appearance_list_libraries`、`zw3d_part_bodies`、`zw3d_file_*` 没有 `inq_`；`zw3d_inq_open_files` 和 `zw3d_file_open` 语义相反却都像动词。所以"看名字猜只读"并不可靠——这恰好是 annotations 该干的事，但它的 annotations 也不可靠。

### 5.3 描述写法（真有值得抄的模版）

它的描述不是"一句话简介"，而是**给 LLM 的操作规程**，典型结构：

```
<一句话做什么>
返回什么：Returns feature_id, face_id, diameter, hole_type, and point_count.
何时用 / 何时不用：ONLY use this for structurally meaningful holes... Do NOT use this to cut away material → use zw3d_sketch_extrude with operation='remove' instead.
前置步骤：Use this after zw3d_inq_shape_faces. Get face_ids from ...
分支引导：If multiple matches are returned, present them to the user and ask which one.
粒度指引：Whole part → this tool; specific faces → that tool; assembly component → that other tool.
```

`zw3d_hole` 的描述把"这是工程孔，不是挖料"写进去了；`zw3d_appearance_search_materials` 写了"多个匹配时不要自己挑，列给用户选"。**"排除错误用法 + 指路到正确工具 + 规定返回值"这三件事，正是 LLM 最容易犯错的三个点**，这个模版直接可搬。

### 5.4 缺陷（可复现）

1. **annotations 语义不可信**：纯读工具被标成写。例如 `zw3d_appearance_list_libraries`（列出材质库）、`zw3d_part_bodies`（查询实体）、`zw3d_tool_capabilities`（返回能力边界）都是 `readOnlyHint:false`；反过来 `zw3d_inq_open_files` 是 `readOnlyHint:true`。宿主靠 annotations 判断"能不能自动执行/要不要弹确认"，标错会直接影响安全策略。
2. **没有 outputSchema**：所有结果都是 `content[0].text` 里塞一段 JSON 字符串（实测 `zw3d_status` 返回的是 `"{\n  \"server\": ...}"` 这种**字符串化 JSON**），客户端得二次解析；而且没有机器可验证的返回契约。
3. **文档漂移**：59 条描述里有 **5 个引用不存在的工具**——`zw3d_model_state`（被引用 2 次，原文还写"Prefer GFS (zw3d_model_state query_faces) over this tool"）、`zw3d_inq_components`（3 次）、`zw3d_comp_insert`、`zw3d_constraint_add`、`zw3d_exit_active_sketch`。模型按提示去调就会撞 "Unknown ZW3D tool"。这类漂移**可以靠一条测试自动拦住**（本次已在你仓库里实现）。
4. **载荷失控**：95 KB 的 `tools/list`，多数宿主会把它整段放进上下文 → 每轮对话都在为这 95 KB 付费。
5. **内部细节外泄**：失败正文把 `ipc_error: "Failed to open event. Error: 2"` 直接给模型看，模型既不懂也无从处置；更好的做法是"人话 + 结构化 error_code"。

### 5.5 亮点：`zw3d_tool_capabilities`（元工具）

这是全篇最值得抄的一个设计。它返回的是**能力边界知识库**，不是几何：

```json
{"zw3d_sketch_create": {
   "cpp_entry": "~SKETCH_CREATE -> cvxPartSkNew/cvxPartDatum+cvxPartSkPlane",
   "supports":  ["2D profiles on standard planes XY/XZ/YZ", "sketching directly on a planar face", ...],
   "boundaries":["this tool only creates a sketch; it does not create or modify solid bodies",
                 "never pass plane='{\"base\":\"XY\"}'; that is a string, not an object"],
   "verify_after":["confirm sketch_id (and datum_id ...) before relying on the sketch"]}}
```

它把"底层实现到哪一步、哪些参数写法一定错、做完之后该验证什么"显式交给模型。**你的 creo-mcp 恰好有大量这类知识**（VB API 13.4 没有公开的 extrude/revolve 指令工厂、只能走 `CreateUDFGroup`、`.prt` 不能自动判定原生/NX、`.gph` 不能无清单放置……），这些现在只写在 `ARCHITECTURE.md` 里让模型"自己去看文档"，做成工具返回会可靠得多。

---

## 6. 协议表面

| 项 | 中望 | 说明 |
|---|---|---|
| `initialize` | 返回 `protocolVersion: 2025-06-18`，capabilities 含 `experimental/prompts/resources/tools` | 标准 |
| `instructions` | 230 字符，只有 3 条纪律（串行、先查 ID、圆角倒角放最后） | 偏薄 |
| tools | 59，全部有 `title` + `annotations` + `inputSchema` | 无 `outputSchema` |
| resources | `zw3d://server/status`、`zw3d://tools/catalog`、`zw3d://session/history` | **status / catalog / 调用历史都能作为资源读**，这条很好 |
| prompts | `zw3d_modeling_assistant`、`zw3d_debug_failed_command` | **"调试失败调用"的 prompt 是个低成本高收益的点子** |
| transport | `--transport {stdio,streamable-http}`，`--host/--port`，`--session-id` | 支持远程/GUI 场景 |
| 自检 CLI | `--list`（人读）、`--list-json`（机读）、`--help` | 维护者/CI 友好 |
| 错误协议 | 工具级 `isError:true` + JSON 正文；未知工具也走 `isError` 而非 JSON-RPC `-32602` | 偏差但可接受 |

实测中 `zw3d_status` 不依赖 CAD（能报 `zw3d_found` / `zw3d_runtime_dir` / `zw3d_exe` / `remote_found`），`zw3d_inq_open_files` 在无会话时返回 `isError:true` + `ipc_error`。**"元工具永远可用、业务工具明确失败"** 这个约定值得学。

---

## 7. 商业化层（看，但别照搬）

`.env` + DLL 字符串显示了完整链路：

- OAuth：`ZW3D_MCP_AUTH_ISSUER=https://accounts.zwsoft.cn`（IDS4），public client + PKCE，回调用自定义协议 `zw3dmcp://oauth/ids4/callback`（注册到 HKCU，**不占本地端口**），scope `ZW3D.MCP`，token 落 `%LOCALAPPDATA%\ZWSOFT\ZW3D_MCP\oauth_tokens.json`。
- 计量：`ZW3D_MCP_GATEWAY_BASE=https://ai-gateway.zwsoft.cn/`、`ZW3D_MCP_USAGE_ENABLED=1`、`*_FEATURE_KEY/PRODUCT_CODE`、`usage_event_type=mcp_session_usage`；**首次建模命令时由 C++ 侧校验授权并上报用量**。
- 设计意图很明确：**元工具/`--list` 不校验授权**（所以你能离线把工具列出来），**真正建模才查 license**——这是"先让用户看到价值、再要求授权"的商业漏斗。

对你的意义：自用/内部项目不需要这套；但如果将来要分发给外部用户，这套"OAuth + 网关计量 + 把 license 校验放在离 CAD 最近的一层"的骨架可以直接参考。**不建议**照搬的是：把授权校验塞进 DLL（导致离线不可用、排障困难）。

---

## 8. 它"完善"吗？分维度打分

| 维度 | 评分 | 依据 |
|---|---|---|
| 分发 / 安装 / 升级 | **A** | 注册表探测、多版本家族、npm 缓存、哈希比对部署、EPERM/EBUSY 分类、可粘贴修复命令 |
| 启动前预检与失败 UX | **A** | 拦截"连上必坏"状态；用 `initialize` 错误回中文可操作指引；`ALLOW_DEGRADED` 逃生门 |
| 工具设计（可用性） | **B+** | 命名成体系、描述是操作规程、有 capabilities 元工具；但 59 个工具偏碎、20 参数的 `pattern` 对 LLM 不友好 |
| 工具设计（治理） | **C** | 无 outputSchema；annotations 标错；5 处文档漂移；内部错误码外泄 |
| 协议规范度 | **B-** | resources/prompts 都有，但 instructions 薄、无结构化输出、错误码不区分机器可处理与不可处理 |
| 可维护性 | **B** | 模块按域拆分（`tools/zw3d_commands/*` + registry + validators + command_runner）是好架构；但**发行物不可读**，且没有可见测试 |
| 可观测性 | **A-** | 按天日志、stderr 纪律、status 资源、会话调用历史资源；缺 trace id / 耗时分布 |
| 测试 | **？** | 发行包里没有测试，无法评估（这是它最大的"黑箱"） |

一句话：**"运维外壳"是教科书级，"协议与工具治理"只是及格偏上**。它不是"完美范式"，而是"一个真实商业产品在分发可靠性上砸了重资源、在协议细节上偷了懒"的典型样本。

---

## 9. 它的设计思路成立吗？

**成立，但有前提。** 它的三大设计选择都对应明确的商业约束：

| 设计 | 成立的前提 | 换到你的场景 |
|---|---|---|
| Node 启动器负责装/修/拦 | 要给**非技术用户**一键装（npm/npx 一条命令），且必须往受保护目录写 DLL | 你的 Python 包不需要写 `Program Files`，但**"启动前自检 + 明确失败"**同样需要，只是可以放进 `--doctor` |
| 常驻 DLL + 共享内存 IPC | CAD 只提供进程内 C++ API | Creo 有 COM，隔离进程更划算 → **不抄** |
| 59 个扁平工具 | 目标是"覆盖全功能 + 单工具语义清晰"，且宿主能承受大工具面 | 你的 27 个已经够用，**应该靠"域分组 + 元工具 + 描述模版"提质量，而不是加数量** |

它**没有解决**（也说明"借鉴 ≠ 照抄"）的三件事，恰好是你已经做对的：

1. **返回契约**：没有 outputSchema → 客户端无法校验、无法做类型化渲染。
2. **安全语义**：annotations 乱标 → 宿主无法正确区分"只读 / 会改模型 / 会写文件"。
3. **可回归**：没有可见测试与契约测试 → 5 处工具引用漂移就是这么来的。

---

## 10. 有没有更好的做法：目标态

把两边优点拼起来的目标态（这次的落地就是朝这个方向走第一批）：

```text
              ┌─ 分发层：pip/pipx 安装 + `creo-mcp doctor` 预检 + 明确失败（借中望）
              ├─ 协议层：stdio + streamable-http 双传输（借中望）
              ├─ 工具面：少量、强语义、强契约（你的现状）+ 域分组 + 描述模版（借中望）
              ├─ 元工具：status / catalog / capabilities（借中望）
              ├─ 资源：文档（你的现状）+ status / catalog（借中望）
              ├─ 自检：--list / --list-json / doctor（借中望）
              └─ 治理：契约测试（工具命名/描述/annotations/交叉引用/载荷预算）← 两边都没有
```

具体 7 条建议，按性价比排序：

1. **加 `--list` / `--list-json`**：不启传输就能看工具面，CI 可以做快照对比。（本次已实现）
2. **加 `--doctor` 预检**：注册表/loadpoint、COM ProgID、`xtop.exe` 进程、pywin32、operation log 可写、broker 状态、可选真实连接探测，每条带中文修复建议。（本次已实现）
3. **加元工具 `creo_tool_catalog` + `creo_get_capabilities`**：前者让模型"按需查目录"而不是每轮背 127 KB；后者把 VB API 的能力边界（无公开 extrude 工厂、必须 UDF、`.prt` 歧义）显式交给模型。（本次已实现）
4. **加 `creo://server/status` + `creo://tools/catalog` 资源**：与中望对齐，且是"按需读取"的上下文友好通道。（本次已实现）
5. **加"工具契约测试"**：命名前缀、title/描述非空、annotations 与只读性一致、`creo_*` 交叉引用必须存在、`tools/list` 载荷预算。中望的 5 处漂移就是这条测试没写。（本次已实现）
6. **选择性结构化输出（P1，未做）**：现在 outputSchema 占 76.9 KB，其中 `creo_create_product_clearance_tray` 一个就 8 KB。建议只对"模型需要据此分支"的工具（status / structure / find_surfaces / geometry_details）保留 outputSchema，对"一次性大报告"（托盘 plan / handoff report）改成文本 + `creo_get_operation_log` 二次查询；或提供 `CREO_MCP_LEAN_TOOLS=1` 开关。
7. **（P2）多传输与 prompts**：`--transport streamable-http` 已顺手实现；`prompts`（如"调试失败的 Creo 调用"）成本很低，可以照中望加一个。

---

## 11. 本次对 creo-mcp 的落地改动

（详见同目录 `CREO_MCP_AUDIT.zh-CN.md`、`../TOOL_CONVENTIONS.zh-CN.md` 与仓库 README；实现以代码与测试为准。）

- 新增 `creo_mcp/catalog.py`：工具目录构建 + 文本/JSON 渲染 + **契约检查**（含交叉引用漂移检测）。
- 新增 `creo_mcp/diagnostics.py`：`creo-mcp doctor` 的 10 项预检与中文修复建议。
- `creo_mcp/server.py`：新增 `creo_tool_catalog`、`creo_get_capabilities` 两个元工具；新增 `creo://server/status`、`creo://tools/catalog` 两个资源；新增 CLI `--list`、`--list-json`、`--doctor`、`--transport`。
- 新增 `tests/test_tool_conventions.py`：把"规范"变成可回归的断言（命名/描述/annotations/交叉引用/载荷预算）。
- 新增 `docs/TOOL_CONVENTIONS.zh-CN.md`：工具命名与描述模版（把中望的描述模版本地化）。

---

## 附录 A：证据与复现

```powershell
# 1) 启动器源码（可读）
tar -xzf .\zw3d\connector\zw3d-mcp-1.0.2.tgz -C <tmp>; <tmp>\package\index.js   # 1044 行

# 2) 工具面（元工具不依赖 CAD，可离线跑）
& '.\zw3d\connector\已部署\ZW3D_MCP.exe' --help
& '.\zw3d\connector\已部署\ZW3D_MCP.exe' --list          # 人读，59 个工具 + 入参
& '.\zw3d\connector\已部署\ZW3D_MCP.exe' --list-json     # 机读

# 3) 协议表面（stdio JSON-RPC：initialize / tools/list / resources/list / prompts/list / tools/call）
#    实测：tools/list 单行 102,941 字节；59 工具；annotations 59；outputSchema 0
#    实测：zw3d_status 离线可用；zw3d_inq_open_files → isError:true + ipc_error

# 4) 二进制字符串（IPC / 授权 / 模块清单）
#    ZW3D_MCP.exe  → PyInstaller + ZW3D_MCP.tools.* 模块清单 + mcp.server.fastmcp
#    ZW3D_MCP.dll  → ShareMemoryWriter / ApiCommand::call / ZW_API_* 错误码 / ZW3D_MCP_AUTH_*
```

**推断声明**：exe/dll 内部的具体实现（共享内存布局、事件命名规则、C++ 侧如何把 JSON 映射到 `cvxPart*`）属于「推断」，依据是字符串与错误信息，未见源码。工具面、协议表面、入口参数、启动器逻辑、`.env` 配置项均为**直接观测**。

---

## 附录 B：一句话回答"我该抄什么、不该抄什么"

- **抄**：启动预检与"可操作失败"、`--list` 自检、元工具（catalog / capabilities / status）、资源化的 status & 目录、"何时用/何时不用/返回什么/做完验证什么"的描述模版、按天日志与 stdout 纯度纪律。
- **不抄**：59 个扁平工具、95 KB 的 `tools/list`、无 outputSchema 的文本化结果、乱标的 annotations、引用不存在工具的文档、把 license 塞进 DLL、20 个参数的单工具。
- **你有而它没有的**：结构化输出、语义正确的 annotations 分层、96 个测试、`confirmation` 令牌、隔离进程的超时与看门狗。**这些不要为了"对齐"而丢掉。**
