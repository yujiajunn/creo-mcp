# Creo UDF 清单与资格验证

## 用途

Creo VB API 没有公开普通拉伸、旋转等特征的完整创建指令工厂，但官方提供了
`IpfcSolid.CreateUDFGroup`。本 MCP 因此使用“参数化 UDF + 严格 JSON 清单”作为
通用原生特征创建基础。

UDF 是 Creo 原生 `.gph` 文件，负责保存特征定义。清单负责声明该文件经过验证
的使用边界。Skill 可以选择 UDF 和计算参数，但不能跳过清单校验。

## 清单格式

```json
{
  "schema_version": "1.0",
  "udf_name": "node",
  "udf_file": "node.gph",
  "sha256": "64位小写或大写十六进制SHA-256",
  "qualified_creo_versions": ["13.4.1.0"],
  "allowed_model_kinds": ["part"],
  "verified_non_interactive": false,
  "references": [
    {
      "prompt": "REF_CSYS",
      "item_type": "coordinate_system"
    }
  ],
  "variant_dimensions": [
    {
      "name": "d11",
      "unit": "mm",
      "minimum": 1.0,
      "maximum": 100.0
    }
  ]
}
```

`udf_file` 可以是绝对路径，也可以是相对于清单文件的路径。文件名必须与
`udf_name` 一致，扩展名必须是 `.gph`。

可用引用类型为：

- `feature`
- `surface`
- `edge`
- `coordinate_system`
- `axis`
- `point`

当前版本只允许引用目标模型本身的项目，不接受装配组件路径或外部模型引用。
需要这些能力时，应先扩展 MCP 的结构发现与组件路径校验，不能把不完整输入交给
Creo 弹窗补充。

变量尺寸单位为：

- `mm`：请求值使用毫米，MCP 按目标模型长度单位换算后传给 Creo。
- `degree`：请求值按角度直接传给 Creo。
- `unitless`：无量纲数值直接传给 Creo。

每个变量尺寸必须提供有限的最小值和最大值。放置请求必须完整绑定清单中的全部
引用和变量尺寸，不能少传或多传。

`verified_non_interactive=false` 表示候选清单，只能用于受监督资格运行；`true` 表示
已经按下述流程完成真实 Creo 验证，可以用于正式放置。离线校验只证明 JSON、路径、
哈希和契约自洽，不会自动把 `false` 改为 `true`。

## 三阶段调用

第一阶段调用 `creo_validate_udf_manifest`：

```json
{
  "manifest_path": "<UDF 目录>\\node.udf.json"
}
```

该工具不连接 Creo。它严格解析字段、解析 `.gph` 路径、计算完整文件 SHA-256，
并返回 `validation_id`。文件或清单发生变化后，旧 `validation_id` 自动失效。

第二阶段调用 `creo_run_udf_qualification`，对候选清单做一次受监督运行：

```json
{
  "manifest_path": "<UDF 目录>\\node.candidate.udf.json",
  "validation_id": "第一阶段返回的64位标识",
  "model_name": null,
  "model_kind": null,
  "references": [
    {
      "prompt": "REF_CSYS",
      "item_type": "coordinate_system",
      "item_id": 92
    }
  ],
  "variant_dimensions": [
    {
      "name": "d11",
      "value": 25.4
    }
  ],
  "confirmation": "run_udf_qualification"
}
```

该操作只接受 `verified_non_interactive=false`，不会保存模型，也不会修改清单。返回
`candidate_run_completed` 只表示调用、重生成和测量正常返回，不等于已经取得资格；仍需
人工确认 Creo 没有弹出交互界面，且结果几何符合预期。

第三阶段由验证人员把确认过的正式清单改为 `verified_non_interactive=true`，重新执行
第一阶段取得新的 `validation_id`，再调用 `creo_place_qualified_udf`：

```json
{
  "manifest_path": "<UDF 目录>\\node.udf.json",
  "validation_id": "第一阶段返回的64位标识",
  "model_name": null,
  "model_kind": null,
  "references": [
    {
      "prompt": "REF_CSYS",
      "item_type": "coordinate_system",
      "item_id": 92
    }
  ],
  "variant_dimensions": [
    {
      "name": "d11",
      "value": 25.4
    }
  ],
  "confirmation": "place_qualified_udf"
}
```

`model_name` 和 `model_kind` 都为 `null` 时使用当前活动模型；指定模型时二者必须
同时提供。引用 ID 来自 `creo_get_model_structure`。

资格运行和正式放置都会再次校验清单和文件，并检查当前 Creo 安装目录中的精确版本。
全部输入通过后才构造 COM 对象。二者都关闭 Fix Model 和 UDF 重定义交互入口，调用
`CreateUDFGroup`，重生成目标模型，然后返回组成员、包络、体积和 `saved=false`。
正式放置拒绝 `false`，资格运行拒绝 `true`，两条路径不能混用。

## 真实 Creo 资格验证

把 `verified_non_interactive` 从 `false` 改为 `true` 之前，必须在清单声明的每个 Creo 精确
版本中完成以下验证：

1. 从干净测试模型开始，记录目标模型类型和原生单位。
2. 使用 `creo_get_model_structure` 取得真实引用 ID。
3. 通过 `creo_run_udf_qualification` 传入清单声明的全部引用和变量尺寸，不进行任何人工点击。
4. 确认 Creo 没有出现引用、方向、象限、尺寸、失败修复或重定义弹窗。
5. 确认工具正常返回、组成员完整、模型重生成成功且几何符合预期。
6. 对每个边界值和至少一个非法值测试；非法值必须在调用 Creo 前被拒绝。
7. 对最终 `.gph` 重新计算 SHA-256，并把精确版本和哈希写入清单。

Mock 单元测试只能证明参数构造和前置校验，不能替代真实 Creo 资格验证。未经
上述验证的 UDF 不得标记为非交互可用。

## 当前限制

- 不支持外部装配引用、组件路径、方向回答、象限点和装配相交设置。
- 不自动保存模型；保存必须作为后续独立、显式确认的操作。
- `verified_non_interactive` 是人工确认后的工程资格声明，不是工具自动推断结果。
- 工具调用失败后应立即检查当前模型；Creo 可能已经创建部分特征，MCP 不能承诺
  对所有厂商级失败执行事务回滚。

这些限制应保留在 MCP 层，不能由 Skill 规避。
