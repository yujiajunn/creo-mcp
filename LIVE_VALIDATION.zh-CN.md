# Creo MCP 真实会话验证记录

本文只记录在真实 Creo Parametric 交互会话中执行过的结果。单元测试、Mock COM
测试和 MCP Schema 检查不等同于真实会话验证。

## 环境

- 日期：2026-10-08 至 2026-10-09
- Creo：13.4.1.0
- 当时验证的 MCP：`0.9.0`
- 当前验证版本：`0.10.0`
- Windows 用户与 Creo 启动用户相同
- 测试模型：由 `吸塑盒.stp` 导入的未保存零件 `吸塑盒.prt`、由 `针管.stp`
  导入的毫米制装配 `针管.asm`

## 已通过

### 0.10.0 连接探针与 COM 缓存恢复

操作：`--probe-status`

结果：

- 在单个 Creo 13 会话中真实返回 `connected=true`，安装根目录和工作目录读取成功。
- 本次 Creo 没有活动模型，因此 `active_model=null`；连接本身正常。
- 初次连接失败的原始错误为缺少
  `win32com.gen_py...IpfcAsyncConnection`，确认是 Python 3.13 的 `gen_py` 缓存不完整。
- 将该版本缓存目录移动为备份后，`pywin32` 自动重建缓存，连接恢复；无需重新安装或
  注册 Creo VB API，也没有修改任何模型。

### 模型结构读取

操作：`inspect_model_structure`

结果：

- 成功读取模型、特征、表面、边、坐标系和模型参数。
- 模型包含 2 个特征：坐标系和导入特征。
- 表面与边均超过请求上限 200，`truncated_categories` 正确报告截断。
- 匿名或不可读参数不再导致整个操作失败。
- 本次模型没有匿名参数，`skipped_unnamed_parameter_count` 为 0。

### 指定拓扑几何读取

操作：`inspect_geometry_details`

结果：

- 表面 3669 被识别为平面，位于 `Y=0`，面积约 `2393.200342 mm²`。
- 表面 3670 被识别为 NURBS，面积约 `13.579383 mm²`。
- 边 2843、2844 被识别为直线，长度和三个采样点读取成功。
- 两条边的相邻表面 ID 与表面 3669 的拓扑关系一致。
- 所有返回项的 `evaluation_errors` 为空。
- Creo 主单位 API 返回 `mm`。模型参数 `PTC_UNITS_LENGTH` 不能代替主单位 API。

### 表面搜索

操作：`find_surfaces`

过滤条件：平面、面积不小于 `500 mm²`、完整表面位于 `Y=0 +/- 0.01 mm`。

结果：

- 完整扫描 350 个表面。
- 只匹配表面 3669。
- 候选面积、包围盒、所属特征和可见状态与指定拓扑读取结果一致。
- `matched_surface_count=1`，`truncated=false`。

### 原生模型打开

操作：`open_native_model`

模型：PTC VB API 自带的 `plate.prt`。

结果：

- 绝对路径检索失败后，工作目录回退检索成功。
- 返回模型类型为 part，文件名为 `plate.prt`，实例名为 `PLATE`。
- 模型主单位为 `in`，测量结果已标准化为毫米。
- 模型已按请求激活，`source_modified=false`，没有保存或修改源文件。
- 当前会话未取得体积，`volume_mm3=null`；这不影响原生模型检索和激活结果。

### 官方 plate 零件结构

操作：`inspect_model_structure`

结果：

- 成功读取 11 个特征、10 个表面、36 条边、2 条轴和 9 个尺寸。
- 两条轴分别为 `A`（ID 47）和 `B`（ID 72）。
- `coordinate_systems=[]`，该零件不能直接满足 `node.gph` 的 `REF_CSYS` 引用。
- 官方 VB 示例界面默认使用名为 `CS0` 的坐标系；官方样例目录中的
  `component.prt` 包含该名称，将作为下一步候选母件。

### 官方 component 零件结构

操作：`open_native_model`、`inspect_model_structure`

结果：

- `component.prt` 已成功打开并激活，源文件未修改。
- 模型主单位为 `in`，模型类型为 part。
- 坐标系模型项目 `CS0` 的 ID 为 92；其所属坐标系特征 ID 为 91。
- `node.gph` 的 `REF_CSYS` 必须绑定模型项目 ID 92，不能绑定特征 ID 91。
- 官方 `node.gph` 的 SHA-256 已确认为
  `A3239CB7BCB521C6D26094384C1FA72120DF0EBB11F7050A91E62D628529312B`。

### node 候选清单离线验证

操作：`validate_udf_manifest`

结果：

- 候选清单为 `live-validation/node.candidate.udf.json`，资格状态保持为 `false`。
- 成功解析本机官方 `node.gph`，文件大小为 9786 字节，完整 SHA-256 匹配。
- 候选 `validation_id` 为
  `5bf0ce83eef28643405c8cc7d513641be6192590cc9ff4206fe36591558be6e8`。
- 正式放置路径会拒绝该候选清单；只能通过显式确认的资格运行入口使用。

### node 候选资格运行

操作：`run_udf_qualification`

输入：`component.prt`、`REF_CSYS` 绑定坐标系模型项目 ID 92、`d11=25.4 mm`。

API 结果：

- `CreateUDFGroup` 成功返回组 `NODE`，目标模型为 `COMPONENT`。
- 返回 5 个 active 组成员：组标题 94、基准面 95/96/97、切口 99。
- 模型重生成和测量成功；外包络保持不变，符合内部切口不改变外包络的预期。
- `saved=false`，没有保存源模型。
- 返回 `qualification_state=candidate_run_completed`，清单仍为
  `manifest_verified_non_interactive=false`，工具没有自动授予工程资格。
- 操作者已确认整个运行期间没有 Creo 弹窗，也没有人工点击或补选引用。
- 尚未完成尺寸最小值、最大值和非法值的真实会话验证，因此候选清单继续保持
  `verified_non_interactive=false`，暂不生成正式清单。

### 针管装配交付报告与维护元数据

操作：`handoff_report`、`update_model_metadata`

结果：

- 交付报告读取到装配外包络约 `76.5 x 21.7231 x 40 mm`，4 个特征全部 active。
- 报告识别 1 个导入特征、2 个匿名特征、0 个命名驱动尺寸和缺少自动化归属参数，
  正确返回 `review_required`。
- 报告现单独返回 `imported_features`，即使导入特征已经命名，也继续提示该模型应视为
  参考几何，或必须记录可编辑重建策略，不能误判为原生参数化模型。
- 成功创建 4 个模型参数：`MCP_AUTOMATION_ID`、`MCP_AUTOMATION_REVISION`、
  `MCP_MODEL_ROLE` 和 `MCP_SOURCE_FORMAT`；模型重生成成功且 `saved=false`。
- 再次读取时 4 个参数和值均存在；将 `MCP_AUTOMATION_REVISION` 从 1 更新为 2 时返回
  `created=false`，验证了更新已有参数不会重复创建。
- 导入特征 ID 4 成功从匿名项重命名为 `SYRINGE_STEP_IMPORT`，重生成成功且未保存。
- 组件特征 ID 5/6 尚未确认具体工程含义，因此没有猜测性重命名。

### 针管装配受保护另存副本

操作：`save_model_as`

结果：

- 将活动 `针管.asm` 复制为新模型 `syringe_mcp_validation.asm`，目标目录事先不存在同名
  `.asm` 或版本文件。
- `CopyAndRetrieve` 成功，输出文件为
  `<user-desktop>\CreoMCPValidation\syringe_mcp_validation.asm.1`，大小 68570 字节。
- 新副本成功激活，返回 `source_modified=false`、`saved=true`，原模型文件未被覆盖。
- 从活动副本重新读取到 `SYRINGE_STEP_IMPORT`、全部 4 个 `MCP_` 参数和修订号 2，
  证明内存维护信息进入了新副本。

### 针管装配 STEP 导出

操作：`export_model`、`inspect_step`

结果：

- 从新原生副本导出
  `<user-desktop>\CreoMCPValidation\syringe_mcp_validation.step`，大小 94084 字节。
- 返回 `source_modified=false`、`saved=false`，导出没有再次保存或修改源装配。
- 离线解析确认文件为 Creo 生成的 `CONFIG_CONTROL_DESIGN` STEP、单位为毫米，包含
  2 个实体、55 个高级曲面、2 个闭合壳体和 2 条装配关系，推断类型为 assembly。
- 导出目标的扩展名自动补全与 `.stp`/`.step` 等价文件防覆盖逻辑在真实 STEP 导出中通过。

### 写操作结构化日志

操作：检查 `%LOCALAPPDATA%\CreoMCP\logs\operations.jsonl`

结果：

- 从执行真实写操作的同一 PowerShell 环境读取到完整 JSONL 记录。
- 参数创建、特征重命名、参数更新、原生另存副本和 STEP 导出的 `operation_id` 均与
  CLI 响应一致。
- `update_model_metadata` 记录 `saved=false`，`save_model_as` 记录 `saved=true`，
  `export_model` 记录 `saved=false`，目标模型与操作语义一致。
- CLI 的 `log.written=true` 和 `log.path` 与实际文件一致。
- Codex 工具进程对用户配置目录使用隔离文件系统视图，因此它从相同文本路径看到另一份
  本地日志；这不是 MCP 丢日志。审计验证必须在实际运行 MCP 的宿主环境中进行。

## 尚未通过真实会话验证

- `creo_place_qualified_udf` 的真实 UDF 放置、重生成和组成员读取。
- UDF 缺少引用、尺寸越界和版本不匹配时的真实 Creo 行为。
- 装配组件路径和外部引用。
- `creo_update_model_metadata` 的真实命名尺寸写入；参数创建/更新和模型项目重命名已通过。
- `creo_get_handoff_report` 对真实失败或抑制特征的报告；匿名和导入特征已通过。
- `creo_save_model_as` 已在装配上通过；零件另存副本仍待真实验证。
- `creo_export_model` 的 IGES、Parasolid 和可选 profile；STEP 已通过。

原生 `plate.prt` 的绝对路径检索会返回 `pfcExceptions::XToolkitNotFound`。0.8.2 的第二条
官方检索路径已在真实会话验证通过：临时切换到模型所在目录，使用
`CreateFromFileName("plate.prt")` 和 `RetrieveModel` 检索，并在 `finally` 中恢复原工作
目录。旧代码曾把 `plate.prt` 作为实例名，导致 COM 描述符生成 `plate.prt.prt`；该问题
也已修正为实例名 `plate`。

Mock 测试已经覆盖 UDF 完整绑定、单位换算、工作目录恢复和不保存约束，但在
PTC 自带样例零件上成功完成一次非交互放置之前，UDF 写入能力不能标记为工程可用。

## 下一验证步骤

1. 离线验证 `live-validation/node.candidate.udf.json`，取得候选 `validation_id`。
2. 接入实际任务时，在干净、未保存的测试模型上完成尺寸最小值、最大值和非法值验证。
3. 只有确认上述步骤后，才把正式清单标记为 `verified_non_interactive=true` 并重新验证。
4. 在带命名驱动尺寸的测试零件副本上验证毫米/英寸尺寸更新。
5. 继续验证零件另存副本、IGES、Parasolid 和可选 export profile。
