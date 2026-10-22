# Creo MCP 客户端接入、兼容性与后续优化

本文说明当前 Creo MCP 是否符合 MCP 规范、它与常见 MCP 实现的关系，以及如何在
nanobot、Codex、Claude Desktop、Claude Code、Cursor 和 VS Code 中接入。

## 结论

当前实现符合本地 MCP 服务器的标准接入方式，可以进入 Windows 本机 Creo 的受控试用：

- 使用标准 MCP `stdio` 传输，客户端以子进程方式启动服务器，不需要监听端口。
- 使用官方 MCP Python SDK 的 `FastMCP` 实现初始化、工具和资源协议。
- 对外提供服务器名称、版本、用途说明、27 个工具以及 5 个中文文档资源。
- `creo_build_geometry` 提供受约束的 JSON 几何程序（box/cylinder/sphere 图元 +
  union/difference/intersect 布尔 + mirror + linear_pattern + transform（绕轴旋转、
  平移）+ source（把已有毫米 STEP 实体作为布尔操作数直接参与运算），毫米单位）：
  先在 OpenCascade 里构建并验证为单一闭合实体（≤2000 mm），再经 STEP 导入为新的已保存
  Creo 零件（特征名 GENERATED_SOLID）。它是实体概念件，不是参数化草图特征。
- 工具具有稳定名称、标题、描述、输入/输出 JSON Schema 和行为注解。
- 只读操作与写操作分离；写操作要求精确确认值，默认不保存且不覆盖现有文件。
- Creo 侧通过 PTC 官方 VB API / COM 交互，CLI 只是诊断入口，不是另一套业务实现。

“符合 MCP 规范”不等于“与所有市面实现完全相同”。MCP 规定客户端与服务器如何发现和
调用能力，但不规定每个产品必须使用相同部署方式、审批界面或业务工具：

| 场景 | 常见方式 | 当前 Creo MCP |
| --- | --- | --- |
| 单台工作站、本地 CAD | `stdio`，客户端启动本地进程 | 已实现，当前推荐 |
| 多用户、跨机器共享 | Streamable HTTP + 身份认证 | 尚未实现，也不是当前需求 |
| 一键分发 | 安装包、MCP Bundle/桌面扩展或插件 | 尚未封装，当前使用源码和虚拟环境 |
| 具体产品工程规则 | Skill、项目规则或上层工作流 | 后续按针管、吸塑盒等需求创建 |

所以它的协议路线与主流本地 MCP 一致；差异主要在分发和部署成熟度，而不是协议私有化。

## 使用前提

所有本地客户端都必须满足以下条件：

1. Windows 已安装 Creo 13.4.1.0，并安装、注册 `VB API for Creo Parametric`。
2. Creo、MCP 客户端和 Python 进程运行在同一个 Windows 用户和交互式桌面会话中。
3. 建议同一用户只运行一个目标 Creo 会话，避免 COM 连接歧义。
4. 仓库和 Python 路径真实存在；配置中使用绝对路径。
5. 启动 MCP 前先运行连接探针：

```powershell
cd <repo>
.\.venv\Scripts\python.exe .\creo_mcp_server.py --probe-status
```

成功时返回 `"ok": true`。MCP 正常启动后会保持安静，这是 `stdio` 服务器的正常行为；
标准输出是 JSON-RPC 协议通道，普通日志不能写入标准输出。

只使用检查、导入、UDF 和交付能力时安装核心包即可；需要生成概念托盘时安装可选几何
后端：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[concept-geometry]"
```

下文所有配置都使用同一个启动定义：

```text
command = <repo>\.venv\Scripts\python.exe
args[0] = <repo>\creo_mcp_server.py
cwd = <repo>
```

其他电脑安装时，把这三个绝对路径替换为那台电脑上的实际路径。

## 单实例 broker

从 0.12.0 起，MCP 服务器默认不再各自连接 Creo，而是共享一个按 Windows 用户隔离的本地
broker 进程（命名管道 `\\.\pipe\creo-mcp-broker-<用户>`）。broker 独占唯一的 VB API
连接并串行执行所有操作，因此：

- nanobot、Codex、Claude Desktop、Cursor 等客户端可以同时运行；它们通过 broker 排队，
  不再出现 `Creo is busy with another MCP or VB API operation`。
- 重启 MCP 客户端或 nanobot 不会关闭 broker，Creo 连接保持可用。
- 同一时间仍然只有一个 Creo 操作在执行；写操作排队而不是并发执行。
- broker 在第一次需要时自动启动，也可以手动运行 `--broker`。

诊断命令：

```powershell
.\.venv\Scripts\python.exe .\creo_mcp_server.py --broker-status
.\.venv\Scripts\python.exe .\creo_mcp_server.py --broker-stop
.\.venv\Scripts\python.exe .\creo_mcp_server.py --probe-status
```

broker 日志位于 `%LOCALAPPDATA%\CreoMCP\logs\broker.log`。更新 creo-mcp 代码后先执行
`--broker-stop`，下一次调用会自动用新代码启动 broker。需要回到旧的进程内持久 worker
时，设置环境变量 `CREO_MCP_TRANSPORT=worker`。

### 自动显示模式

Creo 13.4 对后台 API 主动创建/激活模型窗口比较敏感，实测会触发
`dbg_err_crash() ... ORDER_DEBUG` 并最终访问冲突。用环境变量
`CREO_MCP_DISPLAY_MODE` 控制：

| 值 | 行为 | 建议 |
| --- | --- | --- |
| `reopen` | 已保存模型先 `Erase` 掉会话内副本，再 `session.OpenFile` 真打开（默认） | 推荐；等同手动“文件→打开”：会话真正激活模型、Navigator 自动切到模型树、点选/右键交互全部正常。未保存的模型（导入、空零件）自动退回 `display_only` |
| `display_only` | 只调用 `pfcModel.Display()` | 半打开状态：`active_model` 为空、点模型无反应（只有旋转可用）、左侧可能停在文件夹浏览器。仅作未保存模型的查看兜底 |
| `none` | 只创建/保存，不碰 Creo 窗口 | 兜底；手动打开保存的文件 |
| `openfile` | 不 Erase 直接 `OpenFile` | 模型已在会话中时是空操作，基本无效 |
| `display` | `Display()` + 重绘当前窗口 | 已知会触发图形错误，不建议 |

`reopen` 模式下，创建/导入/打开工具会自动附带一张 Creo 窗口截图（MCP image content），
Agent 建模过程中直接看到画面；如需单独截图，调用 `creo_capture_screenshot`。截图通过
`PrintWindow` 直接抓取 Creo 窗口——最小化时会先以不抢焦点的方式恢复窗口，截图本身不切
前台。（真打开会把模型窗口带到前台，这和你手动打开文件的行为一致。）

长操作（方案计算、托盘创建、CAD/STEP 导入、原生模型打开）会发送 MCP
`notifications/progress` 进度通知（0–100，含阶段说明）；客户端在 `tools/call` 里带上
`progressToken` 即可收到。

`reopen` 是真正的打开，Creo 会自动把 Navigator 切到「模型树」，无需手动点页签。

注意：`pfcWindow.ExportRasterImage` 在 Creo 13.4 上对隐藏窗口实测会报
`XToolkitGeneralError`，进一步调用还会挂起 Creo，当前不要用于自动预览。

## nanobot

推荐通过 WebUI 的 MCP 设置添加。也可以在 nanobot 停止运行时，将下列内容合并到现有
`~/.nanobot/config.json`；不要覆盖文件中的其他配置：

```json
{
  "tools": {
    "mcpServers": {
      "creo": {
        "type": "stdio",
        "command": "<repo>\\.venv\\Scripts\\python.exe",
        "args": [
          "<repo>\\creo_mcp_server.py"
        ],
        "cwd": "<repo>",
        "toolTimeout": 150,
        "enabledTools": ["*"]
      }
    }
  }
}
```

`enabledTools: ["*"]` 会同时注册工具、Resources 和后续可能增加的 Prompts。如果改成明确
的工具白名单，nanobot 会按设计不加载 Resources 和 Prompts。

保存后重启 gateway，在 MCP 管理页检查连接状态和工具目录。写操作仍应保留客户端审批。
Creo VB API 调用会在本机串行执行；Agent 不应并行调用多个 Creo 工具。工具返回 busy
或超时后应把恢复说明交给用户，不能立即重复调用，也不能自行用 Shell 等待、检查或结束
MCP/Creo 进程。MCP 会在第一次在线工具调用时启动一个持久 worker，并在后续调用中复用
同一个 VB API Session；gateway 或 MCP 服务重启时才会正常断开。

## Codex CLI 与 Codex IDE

Codex CLI 和 IDE 共用 `~/.codex/config.toml`。可直接加入：

```toml
[mcp_servers.creo]
command = '<repo>\.venv\Scripts\python.exe'
args = ['<repo>\creo_mcp_server.py']
cwd = '<repo>'
tool_timeout_sec = 150
```

也可以用 CLI 添加启动命令：

```powershell
codex mcp add creo -- <repo>\.venv\Scripts\python.exe <repo>\creo_mcp_server.py
codex mcp list
```

CLI 添加方式不包含本例的 `cwd` 和超时设置；需要时再在 `config.toml` 中补充。配置变更后
新建 Codex 任务，使工具目录重新加载。

## Claude Desktop

当前可用手工配置方式；面向普通用户分发时，可再封装为 MCP Bundle/桌面扩展。完全退出
Claude Desktop 后，编辑：

```text
%APPDATA%\Claude\claude_desktop_config.json
```

在现有 JSON 中合并：

```json
{
  "mcpServers": {
    "creo": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<repo>\\creo_mcp_server.py"
      ]
    }
  }
}
```

重新启动 Claude Desktop 后，在工具列表中检查 `creo`。连接问题可查看：

```text
%APPDATA%\Claude\logs
```

## Claude Code

在项目目录中执行：

```powershell
claude mcp add creo --scope local -- <repo>\.venv\Scripts\python.exe <repo>\creo_mcp_server.py
claude mcp list
```

需要所有项目可用时，把 `--scope local` 改为 `--scope user`。也可使用 `--scope project`
生成团队可共享的项目配置，但共享前必须把机器相关绝对路径改成团队约定的安装位置。

## Cursor

全局配置文件是 `%USERPROFILE%\.cursor\mcp.json`；仅当前项目使用时，创建
目标项目的 `.cursor\mcp.json`。内容如下：

```json
{
  "mcpServers": {
    "creo": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<repo>\\creo_mcp_server.py"
      ]
    }
  }
}
```

重启 Cursor，在 Agent 的 MCP 工具列表中启用 `creo`。Cursor 默认会在调用工具前请求
批准；不要为仍在验证期的 Creo 写操作全局开启自动批准。

## VS Code / GitHub Copilot

在工作区创建 `.vscode/mcp.json`：

```json
{
  "servers": {
    "creo": {
      "type": "stdio",
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<repo>\\creo_mcp_server.py"
      ]
    }
  }
}
```

在命令面板运行 `MCP: List Servers` 检查并启动服务，然后在 Chat 的工具选择器中启用
Creo 工具。工作区配置可以提交给团队，但绝对路径需要统一或改成适合团队的变量方案。

## 标准协议检查

本仓库的单元测试验证服务器身份、31 个工具、Schema、注解和 5 个 Resources。还可以用
MCP Inspector 做与具体客户端无关的协议检查。以下命令需要本机 Node.js/npm：

```powershell
npx -y @modelcontextprotocol/inspector --cli `
  <repo>\.venv\Scripts\python.exe `
  <repo>\creo_mcp_server.py `
  --method initialize

npx -y @modelcontextprotocol/inspector --cli `
  <repo>\.venv\Scripts\python.exe `
  <repo>\creo_mcp_server.py `
  --method tools/list

npx -y @modelcontextprotocol/inspector --cli `
  <repo>\.venv\Scripts\python.exe `
  <repo>\creo_mcp_server.py `
  --method resources/list
```

这三项分别检查初始化握手、工具目录和资源目录。Creo 业务调用还需要运行中的 Creo 会话。

## 给其他电脑或团队安装

当前最稳妥的交付方式是受控源码安装：

1. 在目标 Windows 工作站安装并注册 Creo VB API。
2. 部署固定版本的 `creo-mcp` 独立仓库。
3. 创建独立 Python 虚拟环境并安装该目录的依赖。
4. 先运行 `--probe-status`，再运行自动测试和 MCP Inspector。
5. 按目标客户端写入绝对 `command`、`args` 和可选 `cwd`。
6. 用只读工具完成冒烟测试，再逐项批准写操作。

如果需要发给大量非开发人员，再做分发层，而不需要重写 Creo 工具：

- 将 Python 环境和启动器固定版本，形成内部安装脚本或签名安装包。
- 为 Claude Desktop 等客户端封装 MCP Bundle/桌面扩展。
- 如果要跨机器、多用户共享，新增 Streamable HTTP 服务、身份认证、授权和主机调度。
- Skill 与 MCP 分开分发：MCP 提供通用 Creo 能力，Skill 提供针管、吸塑盒和企业工程规则。

## 后续按需优化

以下事项不阻塞当前粗版建模和受控试用，按真实项目优先级推进：

- 完成命名尺寸、零件另存副本、IGES、Parasolid 和正式 qualified UDF 的真实会话验证。
- 增加通用原生特征创建、模型树分组、层、关系和设计意图检查能力。
- 将 `operation_id` 直接带入每个 MCP 工具结果，并增加日志轮转、筛选和跳转能力。
- 增加自动化特征组的查找、修改和替换，使重复运行具备更强幂等性。
- 建立版本化安装包、升级/回滚说明和客户端兼容性测试矩阵。
- 需求出现后再评估远程 Streamable HTTP、OAuth、队列和多 Creo 会话调度。

已验证与未验证能力的唯一事实来源是
[`LIVE_VALIDATION.zh-CN.md`](LIVE_VALIDATION.zh-CN.md)；工程交付门槛见
[`ENGINEERING_STANDARD.zh-CN.md`](ENGINEERING_STANDARD.zh-CN.md)。不能仅因协议连接成功，
就把尚未真实验证的 CAD 工作流视为生产合格。

## 官方参考

- MCP Python SDK：<https://py.sdk.modelcontextprotocol.io/get-started/real-host/>
- MCP Inspector：<https://github.com/modelcontextprotocol/inspector>
- OpenAI Codex MCP：<https://developers.openai.com/learn/docs-mcp>
- Anthropic 本地 MCP：<https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop>
- Cursor MCP：<https://docs.cursor.com/context/model-context-protocol>
- VS Code MCP：<https://code.visualstudio.com/docs/agent-customization/mcp-servers>
