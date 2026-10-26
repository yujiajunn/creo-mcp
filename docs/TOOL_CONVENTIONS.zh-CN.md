# Creo MCP 工具契约规范

> 适用对象：`creo_mcp/server.py` 中对外暴露的每一个 MCP 工具（当前 30 个：27 个能力工具 + 3 个元工具）。
> 强制手段：`tests/test_tool_conventions.py`（不通过即视为回归）+ `creo-mcp --list-json`（人工核对）。
> 本规范部分借鉴中望 ZW3D MCP 的写法（工具描述规程、元工具、catalog 自检），但**明确不采纳**其扁平 59 工具、无结构化输出、annotations 语义混乱的做法，理由见 `docs/reference/ZW3D_MCP_ANALYSIS.zh-CN.md`。

---

## 1. 命名

```
creo_<动词>_<对象>[_<限定语>]
```

- 前缀固定 `creo_`，全小写 snake_case，不含版本号、不含产品名、不含缩写歧义。
- **动词白名单**（新增工具必须落在表内；不在表内需要在评审里说明理由并扩展本表）：

| 类别 | 动词 | 对应 annotations |
|---|---|---|
| 只读 / 分析 | `get`、`list`、`find`、`inspect`、`validate`、`plan`、`capture` | `READ_ONLY_ANNOTATIONS` |
| 会改会话 / 写文件 | `create`、`update`、`save`、`export`、`place`、`run`、`open`、`import`、`build` | `WRITE_ANNOTATIONS` 或 `MODEL_EDIT_ANNOTATIONS` |
| 元工具（豁免） | `creo_tool_catalog` | `READ_ONLY_ANNOTATIONS` |

- `plan_*` / `validate_*` 一律只读：它们只做计算与校验，**不得触碰 Creo 模型或磁盘**。干跑语义（`dry_run_only` / `CONCEPT_ONLY`）必须在返回体里显式声明。
- 对象名用业务名词（`model`、`surfaces`、`step_file`、`udf_manifest`），不要用实现名词（`com`、`vbapi`、`proe`）。

**反例**：`creo_capture_creo_window_png`（把实现细节写进名字）、`creo_do_import`（动词模糊）、`creo_get_thing`（对象模糊）。

---

## 2. 每个工具必须携带的元数据

| 字段 | 要求 | 校验 |
|---|---|---|
| `name` | 符合第 1 节 | 测试 |
| `title` | 人类可读的 2–5 词标题，标题式大小写 | 测试 |
| `description` | 非空，且 ≤ 1200 字符；结构见第 4 节 | 测试（长度/非空） |
| `annotations` | 三个常量之一，且与实际行为一致 | 测试（与动词类别交叉校验） |
| `structured_output` | 返回 pydantic 模型的工具必须 `True`；只返回 `Image` 的例外；元工具（`creo_tool_catalog`、`creo_get_capabilities`）允许不带或只带极简（≤512 B）输出 schema，以保持 `tools/list` 轻量 | 测试 |
| 确认令牌 | 任何 `WRITE_*` / `MODEL_EDIT_*` 工具都必须要求在入参里给出字面量确认值 | 测试 |

`description` 上限 1200 字符不是拍脑袋：中望的单条描述最长 2825 字符、59 条合计 35 KB，直接导致 `tools/list` 膨胀；我们的中位数是 270 字符，维持在千字符级即可把"排除错误用法"讲清楚。

---

## 3. annotations 判定规则

| 常量 | 语义 | 判定标准 | 当前使用者 |
|---|---|---|---|
| `READ_ONLY_ANNOTATIONS` | 不改 Creo、不写盘，可重复执行 | 只读模型/只算不写 | `creo_get_*`、`creo_find_*`、`creo_inspect_*`、`creo_validate_*`、`creo_plan_*`、`creo_capture_screenshot` |
| `WRITE_ANNOTATIONS` | 会改会话状态或写文件，但不是破坏性几何编辑 | 新建未保存模型、导入、打开、导出、保存、放置 UDF | `creo_create_*`、`creo_import_*`、`creo_open_native_model`、`creo_save_model_as`、`creo_export_model`、`creo_place_qualified_udf`、`creo_run_udf_qualification`、`creo_build_geometry` |
| `MODEL_EDIT_ANNOTATIONS` | 会改已有模型的几何/参数（破坏性） | 修改既有模型内容 | `creo_update_model_metadata` |

硬性规则：

1. `readOnlyHint=true` 的工具**禁止** `destructiveHint=true`。
2. `readOnlyHint=false` 的工具**禁止**省略确认令牌。
3. `idempotentHint` 只在"同参数重复调用结果一致且不叠加副作用"时为 `true`；写文件/写模型一律 `false`。
4. `openWorldHint` 恒为 `false`（本服务不访问开放世界；`creo_import_cad` 读本地文件不算 open world）。

---

## 4. 描述模版（四段式，按需裁剪）

借自中望 ZW3D MCP 的写法，但压缩到 1200 字符内：

```text
<一句话做什么，含关键约束词（only / must / never）>。
返回：<字段清单或结构化结果要点>。
何时用：<典型意图>；何时不用：<常见误用> → 改用 <正确的工具名>。
前置：<必须先调用的工具或必须满足的状态>；确认：confirmation='<令牌>'。
```

正例（`creo_place_qualified_udf` 现有描述已接近这个结构）：

> Place a qualified UDF ... Requires `verified_non_interactive=true` ... revalidates the exact file hash ... never saves the target.

写描述时的四条纪律：

1. **必须排除最可能的误用**。例如 `creo_build_geometry` 必须写"不要用它拼托盘"，否则模型一定会拼。
2. **必须指路**。`Do NOT ... use <具体工具名> instead`，指路必须指向**真实存在**的工具（测试校验）。
3. **必须说清返回**。模型据此决定下一步；不要写"returns details"这种空话。
4. **不要写实现内部细节**。不出现 COM/ProgID/`pfcls`/进程号/绝对路径模板；这些属于 `--doctor` 与日志。

---

## 5. 交叉引用纪律

- 描述或入参说明里出现的每一个 `creo_*` 标识符，都必须是**当前已注册的工具名**。
- 需要提到"概念"而非工具时，用中文/普通英文（"几何详情"），不要写成疑似工具名的形式。
- 由 `tests/test_tool_conventions.py` 与 `creo_mcp.catalog.find_issues()` 双重校验；中望的 5 处漂移（`zw3d_model_state` 等）就是缺这条约束造成的。

---

## 6. 确认令牌（confirmation token）

- 形式：入参 `confirmation: Literal["<snake_case 令牌>"]`，**唯一合法值**，无默认值。
- 令牌名描述后果而不是动作，例如 `create_new_model`、`import_unsaved_model`、`save_model_as`、`export_model`、`place_qualified_udf`、`run_udf_qualification`、`update_model_metadata`、`update_feature_suppression`、`create_product_clearance_concept`、`open_native_model`。
- 令牌必须在 `description` 中原文写出（`Requires confirmation='...'`），否则模型不知道要传什么。
- 令牌**不是权限系统**：它只保证"调用方明确表达了意图"，真正的保护是工具内部的路径/覆盖/只读校验。

---

## 7. 载荷预算（context budget）

`tools/list` 会被宿主放进上下文，所以工具面是**每轮对话的固定成本**。最近一次实测是
29 工具、`tools/list` 135,749 B（预算 150,000 B，余量约 14 KB）。下表是更早的 27 工具
拆分，只用于看组成比例；**新增工具后必须重跑 `creo-mcp --list-json` 更新这两个数字**：

| 组成 | 字节 |
|---|---|
| 描述文字 | 7,678 |
| inputSchema | 42,866 |
| outputSchema | 76,900 |
| **合计** | **~127 KB** |

预算：

- 单工具描述 ≤ 1200 字符（测试强制）。
- `tools/list` 结果 ≤ **150,000 字节**（测试强制；超预算首先检查 outputSchema）。
- `outputSchema` 选择性使用：**只给"模型需要据此分支"的工具**。当前超标项（`creo_create_product_clearance_tray` 7,991 B、`creo_plan_product_clearance_tray` 5,551 B、`creo_get_geometry_details` 5,235 B）应在后续迭代里改为"精简输出 + 需要时用 `creo_get_operation_log` / 分页查询"。
- 新增工具时先跑 `creo-mcp --list-json`，确认总载荷没有明显上涨。

---

## 8. 错误契约

- **工具级失败**用结构化错误：`code`（稳定字符串）+ `message`（人话）+ 可选 `hint`（下一步该做什么），由 `creo_mcp/errors.py` 的 `CreoMcpError` 承载。
- 不要把底层内部细节（COM HRESULT、管道名、`Failed to open event. Error: 2`）当唯一信息抛给模型；把它放进日志，给模型一句可行动的结论。
- 失败必须是"可重试 / 需用户干预 / 契约错误"三者之一，并让模型能区分：

| 类别 | 例子 | 模型该做什么 |
|---|---|---|
| 可重试 | Creo 忙、超时 | 报告并停下，不要自动重试（instructions 已规定） |
| 需用户干预 | Creo 未运行、模型未激活、权限不足 | 转述修复建议给用户 |
| 契约错误 | 参数非法、引用不存在、未给确认令牌 | 修正入参后重试 |

---

## 9. 新增工具 checklist

1. 名字符合第 1 节，且与既有工具不重复、不近似（避免 `get_geometry` / `get_model_geometry` 这类混淆）。
2. 选对 annotations 常量，确认令牌就位。
3. 描述按四段式写，≤ 1200 字符，指路指向真实工具。
4. 返回 pydantic 模型（`structured_output=True`），或在 `docs/` 说明为什么返回 `Image`。
5. 写单测（schema 校验 + 隔离层 mock）；写工具面的契约由 `tests/test_tool_conventions.py` 自动覆盖。
6. 跑：`pytest -q`、`ruff check`、`creo-mcp --list`、`creo-mcp --doctor`。
7. 需要真实 Creo 的写操作，必须在 `docs/LIVE_VALIDATION.zh-CN.md` 登记验证结果后才能宣称可用。
