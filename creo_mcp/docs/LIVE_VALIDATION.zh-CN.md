# Creo MCP 真实会话验证记录

本文只记录在真实 Creo Parametric 交互会话中执行过的结果。单元测试、Mock COM
测试和 MCP Schema 检查不等同于真实会话验证。

## 环境

- 日期：2026-10-08 至 2026-10-09
- Creo：13.4.1.0
- 当时验证的 MCP：`0.9.0`
- 当前代码版本：`0.12.0`
- Windows 用户与 Creo 启动用户相同
- 测试模型：由 `吸塑盒.stp` 导入的未保存零件 `吸塑盒.prt`、由 `针管.stp`
  导入的毫米制装配 `针管.asm`

## 0.11.0 概念托盘离线验证

- 新增单槽、多槽和组装件粗随形概念规划/创建、逐实体 STEP 测量、包装参考分析及型腔放入检查，工具总数为 31。
- 包装参考分析已对 `吸塑盒.stp` 完成离线验证：只测量单个包装实体，排除 STEP 中辅助几何；
  单位为毫米，实体包络约 `120.983 × 23.213 × 62.983 mm`，成型深度轴为 Y，推断开口
  法向为 `+Y`、开口平面为 `Y=0`，实体体积约 `4937.314 mm³`。该结果用于继承包装约定，
  不得把参考盒尺寸直接复制到新产品包装。
- OpenCascade 后端已离线生成 AP242 STEP，并验证为一个 solid body、一个 closed shell、
  超过 20 个高级曲面；示例包络为 `103 × 64 × 25 mm`，不是实心方块。STEP 已验证携带
  精确 RGB `66, 223, 208`（`#42DFD0`）表面色。
- 创建路径会检查目标名不冲突、导入为单一 Creo BREP 特征、写入完整配方参数、重生成、
  校验包络、保存到新名称，并激活已有或新建模型窗口后执行 `Repaint`。
- 结果固定标记为 `CONCEPT_ONLY`、`imported_brep` 和 `REGENERATE_WITH_MCP`。配方参数不
  直接驱动导入体，不得把它称为原生参数化工程模型。创建路径同时写入
  `MCP_DISPLAY_COLOR_HEX`、`MCP_DISPLAY_FINISH=MATTE_NON_METALLIC` 和
  `MCP_DISPLAY_ROLE=VISUALIZATION_ONLY_NOT_MATERIAL`；这些值不构成材料或表面工艺验证。
- 放入检查的合成 STEP 回归覆盖通过、深度越界和 24 个正交方向搜索。对
  `针管.stp` 使用示例托盘参数、`1.0 mm` 侧向余量和 `0.5 mm` 顶部余量检查，识别到
  2 个实体、24 个方向均不可行，并返回包装状态核对警告。
- 分槽方案的离线回归覆盖两个独立型腔、桥宽和边界验证、单一闭合实体导出、逐实体放入
  检查、Creo 配方参数和导入显示流程。真实 Creo 中的多槽导入仍需用户确认后串行验证。
- 组装件粗随形 R02 已在真实 Creo 13.4.1.0 中验证：源 `针管.stp` 的两个实体接触，按最低
  深度方向生成单个连通阶梯槽，完整实体包含检查零越界；导入后 `2/2` 特征 active，无失败、
  抑制或匿名特征，外形约 `100.9 × 79.9 × 51.9 mm`，结果保持 `CONCEPT_ONLY`。
- 当前只完成离线几何和 Mock 回归验证。真实 Creo 中的导入、保存、即时显示、模型结构
  和交接报告仍需在 MCP/gateway 重新加载 0.11.0 后串行验证。

## 0.12.0 成型过渡修正

- R03 在真实 Creo 中通过完整产品包含、保存和特征状态检查，但用户顶视、斜视和侧视复核
  发现其包围盒式十字轮廓、直壁和硬深度台阶与参考吸塑盒的成型语言不符，因此 R03 只保留
  为被拒绝的问题对照，不能作为视觉验收通过结果。
- 组装件粗随形生成器已改为底部功能轮廓到扩大开口轮廓的放样体，各区域形成概念斜肩，并
  对槽底到侧壁的边增加受底部间隙限制的圆滑过渡；深浅区通过相交放样凸瓣形成连续网络。
- 对 `针管.stp` 的离线 R04 方案验证为完整产品零越界、最大开口外扩 `4.0 mm`、槽底过渡
  半径 `0.75 mm`，导出为一个 solid body 和一个 closed shell，包络约
  `108.154 × 90.100 × 52.386 mm`。
- 84 项完整回归测试通过。R04 尚未通过真实 Creo 导入与三视图人工验收；MCP/gateway 必须
  重新加载 0.12.0 后才能创建新修订。

## 0.12.0 单例 broker 并发验证（2026-10-22）

- 新增按用户隔离的本地 broker（命名管道），独占 VB API 连接；多个 MCP 客户端共享。
- 停用旧的进程内持久 worker 后，broker 在 3.3 秒内连接 Creo 13.4.1.0，活动模型为
  `zhenguan_05ml_tray_c.prt`。
- 同时启动 3 个独立 `--probe-status` 客户端，全部返回 `connected=true`，无 busy 错误。
- 通过真实 MCP stdio 客户端调用 `creo_get_status` 和 `creo_get_model_geometry` 成功；
  活动模型包络约 `95.7 × 59.2 × 22.9 mm`。
- 仍待验证：nanobot 重启后加载新代码的完整对话链路；创建后图形区即时可见性。

## 0.12.0 窗口显示与预览验证（2026-10-22）

- 新增单例 broker 后，多客户端并发探针通过；旧 worker 的独占锁问题解决。
- `pfcModel.Display()` + 重绘在 Creo 13.4 上会触发 `dbg_err_crash() ... ORDER_DEBUG`，
  随后出现访问冲突并退出；`CreateModelWindow` 同样不稳定。
- `session.OpenFile(model.Descr)` 能返回窗口对象，但活动模型仍可能为 `null`，无法可靠
  自动显示；叠加 `window.Activate()` 后仍需继续观察。
- `window.ExportRasterImage` 对 OpenFile 返回的窗口返回 `XToolkitGeneralError`；继续重绘/
  导出会使 Creo 无响应，必须强制重启。当前不采用该离屏预览路径。
- 新增 `creo_capture_screenshot`：通过 `PrintWindow` 直接捕获 Creo 窗口，不切换前台，
  以 MCP image content 返回（PNG，约 55–140 KB），前台窗口保持不变。
- `display_only` 模式（只调用 `pfcModel.Display()`，不重绘、不激活）在真实 Creo 中
  能显示 `ZHENGGUAN_TRAY_R06` 且窗口稳定不崩溃，但后续实测证实它是**“半打开”**状态：
  会话 `active_model` 为 null、图形区点击无反应（只有中键旋转可用）、出现卡住的选中
  高亮、Navigator 不切到模型树。仅适合未保存模型的查看兜底。
- 修复：`reopen` 模式（默认）。对已保存模型先 `model.Erase()` 再 `session.OpenFile(descr)`，
  等同手动“文件→打开”。实测 `active_model` 正确等于 R06、模型树自动显示、图形区交互
  恢复、无 traceback；`open_native_model` 自动附带截图经 MCP 客户端确认画面正常。
- nanobot 一次性 Agent 实测（15:26）：`creo_open_native_model` 返回结果自带截图，
  Agent 未单独调截图工具即确认“左侧模型树完整、无卡住高亮、画面正常”。
- 结论：Creo 13.4 上让模型可交互只能走真打开（Erase+OpenFile）；`Display()` 与任何
  Repaint/窗口导出路径均不可用或半残。若需要 Blender 级的视口控制，仍需进程内
  Toolkit/J-Link add-in。

## 进度通知验证（2026-10-22 15:5x）

- broker 协议新增可选进度帧（`want_progress` opt-in，向后兼容旧客户端/旧 broker）。
- 5 个长操作（plan/create/import_step/import_cad/open_native_model）按阶段上报
  0–100 进度；MCP 工具改为 async + `Context.report_progress` 转发。
- 真实 MCP 客户端实测：`creo_open_native_model` 收到 4 条
  `notifications/progress`（15/50/75/100 含中文阶段消息），结果正常返回。
- 单测 79 个通过（新增 broker 进度帧回路测试与 MCP relay 测试）。

## build_geometry 几何程序验证（2026-10-22 16:2x）

- 新增第 27 个工具 `creo_build_geometry`：受约束 JSON 几何程序（box/cylinder/sphere
  + union/difference/intersect + mirror + linear_pattern），OCP 构建 → 单一闭合实体
  校验（节点/深度/尺寸上限）→ STEP 导出 → 与托盘管线同一条受保护导入路径 → 命名
  特征 GENERATED_SOLID → 保存 + 真打开 + 自动截图。
- 真实端到端：底板挖两个圆槽程序 8.0 秒完成；Creo 实测包络 120×80×12 与程序一致，
  体积 98913.98 mm³，进度通知 7 条，自动预览图确认几何正确，状态栏显示已保存。
- 负例全部被拒并给出可操作原因：负尺寸、多余字段、完全减除、分离双实体、超尺寸、
  镜像未接合、确认值/零件名不合法。单测 88 个通过。

## 0.13.0 DSL v1：source + transform（2026-10-22 17:0x）

- 几何程序新增 `source` 节点（外部毫米 STEP 的实体作为布尔操作数，含 solid_index
  选择与文件大小/后缀守卫）与 `transform` 节点（绕指定轴/定点旋转 + 平移，
  轴与角度必须成对提供）。
- 真实端到端（对用户的 吸塑盒.stp，121×23.2×102.5 mm、350 面单一闭合实体）：
  `difference[source, transform(rotate z 45°, box)]` 斜切一角，9.1 秒完成，
  单一实体校验通过，导入保存为 MCP_GEOM_DEMO_002，7 条进度通知，自动截图确认
  零件完好且切角位置正确（导入后 Creo 按自身默认坐标系报告包络，轴向映射与文件
  坐标系不同，属正常现象）。
- source 文件不存在、solid_index 越界、旋转轴/角度不成对均有明确拒绝；
  单测 94 个通过。

## 0.14.0 干跑方案预览图（2026-10-23 09:4x）

- `creo_plan_product_clearance_tray` 在方案算完后自动附一张**不经过 Creo** 的渲染图
  （等轴测 + 俯视双视图、尺寸标注、插入轴说明）：新模块 `preview_render.py` 用
  OCP 三角化 + PIL 画家算法软着色，方向映射错了（比如把扁平装配立起来装）在干跑
  阶段就能看出来，不用等 90 秒导入 Creo。
- 起因：ASM00_TRAY_R02 因 orientation 映射把 63mm 厚度的扁平装配做成了 142mm 深的
  立式盘；R03 用恒等映射修正后验证干跑预览图与最终 Creo 截图一致。
- 渲染失败绝不拖垮干跑（异常吞掉返回纯方案）；`CREO_MCP_PLAN_PREVIEW=0` 可关闭。
- 真实端到端：00_asm.stp 干跑 57.8s（含二次构形渲染），返回 plan_id + 预览 PNG。
- 托盘方向规则：产品 STEP 在装配/CAD 里的自然平放姿态 → orientation 用恒等映射
  x/y/z；仅当需要改变插入朝向时才用带符号映射。

## 0.10.1 连接加固复测

- 2026-10-09 发现一次 `pfclscom.exe` 静默挂起：Creo `xtop.exe` 仍正常响应，但所有新
  `Connect` 调用无输出且不退出。这与快速报错的 `gen_py` 缓存故障不同。
- 进一步复现确认，问题与 nanobot 的 `cwd`、精简环境、`CREATE_NO_WINDOW` 和 Windows
  Job Object 无关；相同条件的独立探针可以成功，反复 `Connect`/`Disconnect` 后手工和
  nanobot 调用都会进入同一 COM 激活挂起状态。
- MCP 在线调用已改为单一持久 worker：第一次调用建立一次 VB API 连接，后续调用复用
  同一个 Session；超时仍不调用 `kill/terminate`，并熔断后续请求。
- Mock 回归测试覆盖原生超时参数、持久 Session 复用、请求复用同一 worker、互斥忙碌
  返回、并发拒绝、worker 退出后自动解除熔断、连接失败清理和超时只关闭输入；完整套件
  共 65 项并全部通过。
- 已为新建、打开、导入、另存激活、UDF 放置和活动模型元数据更新增加显式窗口
  `Repaint`。Mock 回归测试确认激活和重生成后会请求重绘；仍需在真实 Creo 会话中确认
  几何和模型树无需关闭重开即可立即可见。
- 清理僵死探针后，在同一活动 Creo 会话中连续执行两次 `--probe-status`：第一次约
  13.43 秒、紧接着第二次约 4.32 秒，均返回 `connected=true` 和活动模型
  `SYRINGE_MCP_VALIDATION`，没有再次发生 COM 激活挂起。
- 两次探针均为只读，没有修改或保存模型。
- 旧短连接实现通过 nanobot 连续复现两次启动超时；持久 worker 实现尚需在重启 Creo、
  清除旧 COM 状态后进行真实对话验证。在该项完成前，不把 nanobot 集成链路标记为通过。

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
