# 吸塑托盘分段粗坯——三个真实样本的实测报告

日期：2026-10-26　　Creo：**未启动、未读写**（本报告全部为离线几何计算）

## 1. 这份报告解决什么问题

目标交付是：**工程师给一个待包装器件（STEP），系统自动算出托盘型腔，并且在 Creo 里生成一个"人工可以接手编辑"的原生零件**。

- "算"这一半，仓库里早就有：`creo_mcp/clearance_geometry.py` 按 1 mm 一层（最多 64 层，装配体 24 层）切器件、按完整 BREP 推保守硬边界矩形棱柱，并保证静态包含 + `+Z` 直插。
- "可编辑"这一半以前缺：那 62–1223 个棱柱被一次性烘成**一个**导入实体（`imported_brep`），工程师打开只有一坨 `GENERATED_SOLID`，没有特征树。

本次新增 `creo_mcp/cavity_segments.py`：把棱柱**归并成有限个矩形台阶**，每个台阶 = 模板里一个可命名驱动的原生切除特征。纯 Python、可测、不依赖 Creo。

## 2. 样本与参数（余量为占位值，需厂内确认）

| 文件 | 内容 | 实体会数 | Schema |
|---|---|---|---|
| `针管.stp` | 0.5 mL 针管装配（`_-05ML-0003_ASM`，产品 `250002`/`0009`） | 2 | AP242_MANAGED_MODEL… |
| `00_asm.stp` | 20 件装配（SolidWorks 导出 `SLDASM-PART-*`） | 20 | CONFIG_CONTROL_DESIGN |
| `吸塑盒.stp` | 现有吸塑盒（拿它当"人工成品"对照） | 1 | 同上口径 |

三个文件 `declared_length_unit` 均为 `mm`，符合 `ProductClearanceTrayPlanRequest` 的强制要求。

占位参数：`side_clearance_mm=1.5`、`top_clearance_mm=2.0`、`bottom_clearance_mm=1.5`、`flange_width_mm=10.0`，方向 `tray_x_from="x", tray_y_from="y", tray_z_from="z"`（即器件自身 Z 轴 = 抽拔方向）。归并设置：`max_segments=6`、`min_step_depth_mm=1.5`、`footprint_tolerance_mm=0.05`。

## 3. 实测分段结果（槽位从开口面往下，cum_depth = 距开口面的累计深度）

### 3.1 `针管.stp`　41 层 ×0.9756 mm → 62 棱柱 → **6 槽**，多耗材料 **1.31%**，无需封堵倒扣（`enlarged=[]`）

```text
slot1  L= 80.50 W=24.00  cum_depth=29.41  x=  0.00  y= 0.00
slot2  L= 80.50 W=23.40  cum_depth=31.37  x=  0.00  y=-0.30
slot3  L= 64.50 W=23.40  cum_depth=33.32  x=  8.00  y=-0.30
slot4  L= 10.25 W=21.40  cum_depth=38.20  x= 16.82  y= 0.70
slot5  L=  7.36 W=21.40  cum_depth=40.15  x= 16.87  y= 0.70
slot6  L=  6.20 W=21.40  cum_depth=46.00  x= 17.45  y= 0.70
型腔 z：-44.000 → 2.000 mm　　台阶体积 64 752 → 槽位体积 65 603 mm³
```

### 3.2 `00_asm.stp`　36 层 ×0.9951 mm → 1223 棱柱 → **6 槽**，多耗 **0.44%**，`enlarged=[]`

```text
slot1  L=473.15 W=139.38  cum_depth=31.87  x= -0.01  y= -0.00
slot2  L=148.34 W=133.53  cum_depth=33.86  x=161.56  y=  2.57
slot3  L=141.13 W=104.91  cum_depth=35.85  x=163.96  y= 15.74
slot4  L=133.06 W= 45.21  cum_depth=37.84  x=165.93  y= 38.09
slot5  L= 36.00 W= 12.61  cum_depth=39.83  x=123.39  y= 51.22
slot6  L= 26.00 W= 10.18  cum_depth=41.82  x=128.39  y= 51.20
型腔 z：-39.824 → 2.000 mm　　台阶体积 2 174 670 → 槽位体积 2 184 171 mm³
```

### 3.3 `吸塑盒.stp`（对照现有成品）　62 层 ×1.0 mm → 208 棱柱 → **6 槽**，多耗 **0.69%**，`enlarged=[]`

```text
slot1  L=124.00 W=26.90  cum_depth=54.00  x=  0.00  y=  0.00
slot2  L=124.00 W=26.59  cum_depth=57.00  x=  0.00  y=  0.16
slot3  L=124.00 W=25.32  cum_depth=59.00  x=  0.00  y=  0.79
slot4  L=124.00 W= 8.61  cum_depth=61.00  x=  0.00  y=  9.14
slot5  L=122.00 W= 5.90  cum_depth=66.00  x= -1.00  y= 10.50
slot6  L=114.00 W= 5.90  cum_depth=68.00  x= -3.00  y= 10.50
```

对照样本能对上现有吸塑盒的形态（124 宽、约 66 深、底部收窄成 5.9 的窄槽），说明分段口径和人工设计不矛盾。

## 4. 三个关键结论（都有数据支撑）

1. **K=6 足够，代价极小。** 多耗材料 0.44 %–1.31 %，全部相对"最细分层台阶体积"计算（合并只可能加料，不可能为负）。段数不是精度瓶颈。
2. **槽位不是同心的，必须给每槽位置偏移。** 装配样本 slot1 中心 x≈0，slot2 中心 x≈161.6；针管 slot3 偏 x=8.0。模板若只做"居中槽"，几何就会错。
3. **累计深度天然单调递增**（29.41→31.37→33.32→…，68.00 收尾）。所以模板可以做成**全部从开口面起算的嵌套口袋**：越往里越深，深槽永远落在浅槽范围内 → 直拔不发生机械干涉。三个样本 `enlarged_segments` 全为空，即算法不需要为可用性额外封宽上层。

## 4.1 方向对结果的影响（`00_asm.stp` 实测三组，同一个文件）

| 坐标映射 | 抽拔方向 | 分层 | 棱柱 | 槽数 | 多耗材料 | 单调封堵 |
|---|---|---|---|---|---|---|
| `x,y,z` | 器件 Z（立着抽） | 36 ×0.9951 | 1223 | 6 | **+0.44 %** | 无需 |
| `y,-x,z` | 器件 Z（俯视旋转 90°） | 36 ×0.9951 | 1243 | 5 | +1.03 % | **封了 slot1** |
| `--axes=-x,z,y` | 器件 Y（侧着抽） | 64 ×2.1152 | 714 | 6 | +2.98 % | 无需 |

三条实测结论：

1. **侧抽的深度是立着的 3.5 倍**（139.375 mm vs 41.8 mm 型腔深度），开口从 473 × 139 变成 473 × 40。深拉伸能不能吸出来、材料多用多少，全由这个选择决定 → 必须工艺定，我不能替你选。
2. **分段结果对坐标映射敏感，不只是数字变一变。** 条带切分方向按长边自动选（`clearance_geometry._component_rectangles` 里 `split_x = x_span >= y_span`），俯视旋转 90° 后保守化程度改变，出现"下层台阶比上层宽"，算法把**上层开口封宽**（`enlarged_segments=[1]`，slot1 变成 140.36 × 474.15）而不是生成一个抽不出来的腔。这是正确行为，但也说明：**报给工程师的表必须带方向，换方向要重算。**
3. 同一器件，槽数从 5 到 6 浮动，`--k` 是上限不是配额。

## 5. Creo 模板规格（请在 Creo 里建，一次做完长期用）

新建原生零件模板（建议 `blister_tray_segmented.prt`，mm 模板，工作目录同现有约定）：

```
基准：
  TRAY_ORIGIN_CSYS        右利手，XY = 托盘开口面，Z 朝器件插入的反方向（= 抽拔方向）
  TRAY_MOUTH_PLANE        开口面（所有口袋深度从这里起算）
  TRAY_FLANGE_PLANE       封边面

主体：
  TRAY_BLANK              拉伸凸台 = 外形长 × 宽 ×（封边厚度 + 最深槽深 + 底厚）
                          命名尺寸：TRAY_LENGTH、TRAY_WIDTH、TRAY_BLANK_HEIGHT

每个槽 i = 1..6（拉伸切除，从 TRAY_MOUTH_PLANE 起，方向 -Z，"到指定深度"）：
  SLOT{i}_LENGTH          槽长（X 向）        ← 对应表里的 L
  SLOT{i}_WIDTH           槽宽（Y 向）        ← 对应表里的 W
  SLOT{i}_DEPTH           距开口面累计深度    ← 对应表里的 cum_depth
  SLOT{i}_X_OFFSET        槽中心相对 CSYS 的 X ← 对应表里的 x
  SLOT{i}_Y_OFFSET        槽中心相对 CSYS 的 Y ← 对应表里的 y
  草绘完全由尺寸 + 对称约束定位，禁止拖出来的隐式对齐；槽截面四个角为直角（圆角留给后置特征）

预置但默认抑制（以后要就取消抑制，改尺寸即可，不需要重新建模）：
  FILLET_RADIUS_ALL       槽内直角圆角（边集用组/规则选，别手写边编号）
  DRAFT_ANGLE_ALL         拔模，角度做成命名尺寸 DRAFT_ANGLE
```

共 6 × 5 = 30 个驱动尺寸 + 3 个外形尺寸。**用的槽数 < 6 时抑制多余槽特征**，Skill 会按分段结果决定抑制哪几个。
该动作的执行侧是 `creo_update_feature_suppression`（按特征名定位、重生成后读回状态；
0.16.0 已实现，**尚未通过真实会话验证**，见 `creo_mcp/docs/LIVE_VALIDATION.zh-CN.md`）。

> 为什么把拔模/圆角做成"预置但抑制"：VB API 不能新建原生特征，但**能改已存在特征的尺寸**（`bridge.py` 已在做参数更新与重生成）。所以以后要自动拔模，不用换 Java/J-Link、更不用上 C++ TOOLKIT——取消抑制 + 给角度就行。

## 6. 待你确认的业务参数（我不替你定）

| 参数 | 本次占位 | 要谁定 |
|---|---|---|
| 侧/上/下间隙 | 1.5 / 2.0 / 1.5 mm | 工艺（是否含膜厚补偿） |
| 封边宽度 | 10.0 mm | 热封机夹具 |
| 封边厚度 | 0.8 mm | 膜材 |
| 抽拔方向 | 器件自身 +Z | 每个器件实际装盒姿态 |
| 是否允许 6 槽封顶 | 是 | 复杂异形是否需要 K=8 |

方向尤其重要：`00_asm.stp` 按"器件 Z = 抽拔方向"算出 473 × 139 的大开口，若实际应该侧着抽，方向映射一改，分段表完全不同。

## 7. 复现命令

```powershell
# 纯逻辑回归（13 条，不碰 OCP / 不碰 Creo）
.\.venv\Scripts\python.exe -m pytest tests\test_cavity_segments.py -q

# 对任意器件出分段表（离线只读，需要 cadquery-ocp-novtk，已在 concept-geometry extra）
.\.venv\Scripts\python.exe live-validation\segment_probe.py "<user-desktop>\样本\针管.stp"
.\.venv\Scripts\python.exe live-validation\segment_probe.py "<user-desktop>\样本\00_asm.stp"
.\.venv\Scripts\python.exe live-validation\segment_probe.py "<user-desktop>\样本\吸塑盒.stp"

# 换抽拔方向 / 改间隙（方向一改，分段表完全不同）
.\.venv\Scripts\python.exe live-validation\segment_probe.py "<user-desktop>\样本\00_asm.stp" `
  --axes y,-x,z --side 2.0 --k 8 --min-step 2.0
```

脚本参数：`--k`（槽数上限）、`--min-step`（薄台阶合并阈值 mm）、`--tolerance`（轮廓相同判定 mm）、`--side/--top/--bottom`（间隙）、`--wall/--floor/--flange-thickness/--flange-width`、`--axes`（器件三轴 → 托盘 X/Y/Z，第三位 = 抽拔方向）、`--part`、`--out`、`--json`。输出为 ASCII 表格 + `*.segments.json`。

> 值以 `-` 开头时（例如负轴 `-x,z,y`）必须写成 `--axes=-x,z,y`，写成 `--axes -x,z,y` 会被 argparse 当成一个选项而报错。

## 8. 下一步（按顺序）

1. 你按 §5 在 Creo 里建 `blister_tray_segmented.prt`（预计 30–60 分钟，一次性的）。
2. 我把 `analyse_product` 包成 MCP 只读工具（`creo_plan_tray_segments`），工具面 29 → 30，同步更新 `tools/list` 体积预算与 `--list` 文档。
3. 我做"按分段表驱动模板尺寸 → 抑制未用槽 → 重生成 → 用 `creo_get_model_structure` 复核真实几何 → 存副本"的原生生成链路（写操作，每次要你显式确认）。
4. 用三个样本各验一次，比对原生零件的实际型腔与分段表；差异 >0.05 mm 视为失败。

## 9. 实机粗稿验证（2026-10-26 10:35，走的是当前 MCP，不是分段模板）

用 §2 的占位参数在真实 Creo 13.4 会话上出了针管器件的粗稿（`via: broker`，退出码 0）：

```text
plan_id                 = 997168ffbfd8f850        insertion_path_verified = True
created_model           = zhengguan_tray_draft_r01.prt  (instance ZHENGGUAN_TRAY_DRAFT_R01)
model_path              = <user-appdata>\Local\Temp\creo-tray-draft\zhengguan_tray_draft_r01.prt.1  (353,515 B)
measured_extent_mm      = 102.500 × 46.000 × 45.800   == expected_extent_mm（逐位相同）
model_state             = CONCEPT_ONLY            creo_geometry_kind = imported_brep
source_product_sha256   = 3458d0fb…403ca494       generated_step_deleted = true
回读结构                 = features 2（坐标系 + PRODUCT_CLEARANCE_*）、PTC_VAL_IMP_NUMBER_OF_SOLIDS = 1
```

三点要记住：

1. **`output_directory` 必须预先存在**，否则直接报 `output_directory does not exist or is not a directory`（不会替你建目录）。
2. **`activated: true` 不等于窗口已前置**。`creo_get_status` 实测 `active_model: null`：显示只走 `pfcModel.Display`，故意不用 `CreateModelWindow`/`Window.Activate`（会把 Creo 13.4 图形状态搞坏）。回读请用 `model_name` + `model_kind` 成对点名，空 payload 会报 "not loaded in the session"。
3. 分段表与实机外形**互相咬合**：`80.50 + 2×10 封边 + 2×1.0 壁 = 102.50`、`24.00 + 20 + 2 = 46.00`、`44.0 腔深 + 1.0 底 + 0.5 保守 + 2.0 顶隙 → 45.80`。所以 §3 的槽位表可信。

另外澄清一个我先前说错的：`static_outside_volume_mm3 = 0.0` **不是"体积没回填"**，它是违规体积度量（`clearance_geometry.py:500-509`：产品落在型腔外的体积，超容差直接抛错）。0.0 恰恰是"完全包含"的通过值。`insertion_sweep_outside_volume_mm3 = 0.0` 同理是"直拔无阻挡"。字段语义已补进 `models.py` 的 description。

除本节记录的一次粗稿创建外，本报告其余内容均为离线计算，未修改或保存任何既有模型；`zhengguan_box_r01.prt` 未被改动。

## 10. 重要更正：§3/§4 的"多耗材料"只是合并成本，不是真实超切

`cavity_segments.py` 后续补了两个能力，实测结果推翻 §4 的结论：

1. **按口袋分列聚类**（`_cluster_rects` + 列跟踪）：同一层不再取"所有棱柱的并集包围盒"，而是按互相接触的轮廓分成多个口袋列。修复的正是 §9 提到的超切来源之一。
2. **精确 union 基线**（`_union_area`，X 向扫描线）：算出"规划器在 Creo 里真正切掉的型腔体积"，也就是每条 2 mm 窄条带矩形的并集，而不是它们的包围盒。

### 10.1 union 基线经实机校验（这是本报告可信度的支点）

| 口径 | 数值 |
|---|---|
| 离线算出 `cavity_union_volume_mm3` | **41,873 mm³** |
| 实机反算真实型腔（`102.5×46×45.8 = 216,094` − `PTC_MP_VAL_IMP_VOLUME = 175,592`） | **40,502 mm³** |
| 偏差 | **3.4 %**（来自型腔底 −44.0 与零件底面 −43.8 之间那层薄板） |

所以离线几何与 Creo 实际产物是同一套东西，误差在毫米级量化范围内，§3 的槽位表可以用。

### 10.2 真实超切率（K=6，占位余量）

| 样本 | 真实型腔 union | 最细台阶（每层包围盒） | K=6 槽位 | **真实超切** | 口袋列 |
|---|---|---|---|---|---|
| `针管.stp` | 41,873 | 63,679 | 64,772 | **+54.69 %** | 2 |
| `吸塑盒.stp` | 156,262 | 201,975 | 203,650 | **+30.33 %** | 1 |
| `00_asm.stp` | 454,801 | 2,038,772 | 2,054,140 | **+351.66 %** | 3 |

§3/§4 里写的 `+0.44 % … +1.31 %` **仍然成立但含义不同**：那是"K=6 相对最细台阶"的**合并成本**。实测 K=64 时该项为 **+0.00 %**，说明槽数上限根本不是瓶颈；瓶颈是"每一步只能用一个矩形"这个表示法本身。

### 10.3 诊断与修法（两个独立的杠杆）

1. **丢掉的是"器件归属"**。`build_cavity_prisms` 把每个实体的棱柱拍平成同一个列表，规划器本来按组件分别算矩形（`zip(meshes, strip_widths)`），我这边却把 20 个器件的轮廓并成 3 列 → 装配样本顶层出现 `473.15 × 139.38` 一整块，把器件之间的空气全吃掉。**修法：棱柱带 component 索引，按器件分列**。一个 20 件吸塑盘本来就该是 20 个独立型腔，这也和实际工艺一致。预计把 +351 % 压到单器件量级（+30 %～+60 %）。
2. **单矩形表示不了 L/T 形轮廓**。针管 54 % 与吸塑盒 30 % 属于这一类。**修法：每步内部做最大矩形分解**（占用栅格贪心覆盖，条带宽度已知 2 mm，栅格约 40×12），一步出多个槽，每个都贴合轮廓；代价是槽数上升。

两个修法都在 `cavity_segments.py` 内部，不改规划器、不碰 Creo。**杠杆 1 已实现并实测，结论是负面的（见 §10.5，默认关闭）；真正剩下的是 §10.6。**

### 10.4 回归覆盖

新增 10 条测试（并列表分离、`_union_area` 四种几何、L 形 union < 包围盒、owners 长度校验、单 owner 与无 owner 等价），本模块 **23 passed**；全仓 **175 passed**；新文件 ruff 0 违规。`SegmentPlan` 现在同时报 `cavity_union_volume_mm3`（真实）、`reference_staircase_volume_mm3`（每层包围盒）、`segment_volume_mm3`（K 槽）、`over_cut_ratio`（相对真实）、`pocket_columns`、`device_count`、`cross_column_merges`，并把旧字段 `waste_ratio` 保留为同一口径的别名。

### 10.5 杠杆 1（按器件分列）实测：**负面结果，默认关闭**

`consolidate_prisms(..., owners=...)` 与 `analyse_product(..., per_device=True)`（CLI `--per-device`）已实现并测过，但拿真实样本一跑，结论是**它不解决超切，多数情况反而更差**：

| 样本 | 默认（整体一池） | `--per-device` |
|---|---|---|
| `针管.stp`（2 实体） | 2 列，**+54.69 %** | 3 列，**+64.88 %**（更差） |
| `00_asm.stp`（20 实体，K=6） | 3 列，**+351.66 %** | 28 列，+354.37 %，且 `cross_column_collapses=[1,4,5,6]` |
| `00_asm.stp`（K=120，无跨列塌陷） | — | 28 列，+281.23 % |

原因看得很清楚——**这些实体在俯视上是互相重叠/堆叠的，不是并排摆着的**：`00_asm` 里 device1 的包围轮廓 98.84 × 135.88 与 device3 的 87.69 × 109.25 大面积重合；device5/device6 都是 350.8 × 9.4 的长条，device10 有 375.6 × 6.6——那是**横跨整个托盘的长杆件**。按器件分列时每个零件都要为自己的那一层出一个包围矩形，重叠区域的空气被**重复计算多次**，总体积反而涨。K=6 时更糟：28 个列抢 6 个槽，触发跨列塌陷，大包围盒又长回来了。

所以：`per_device` 保留为**显式选项**（默认 `False`，即保持原有行为），只有当"包装单元确实是若干个并排的独立器件"（例如一格一件的吸塑盘）时才开。

**这把问题推回到一个业务判断上**：`00_asm.stp` 是"一个由 20 个零件组成的产品"（→ 单腔，28 列没意义），还是"20 件独立器件排在托盘上"（→ 每器件一腔 + 阵列）？`针管.stp` 的 2 个实体（管身 + 芯杆）显然是**套件内叠放**，所以它该走单腔——实测也证明单腔更好（+54.69 % < +64.88 %）。

### 10.6 真正剩下的杠杆

超切的**全部**来源是"每一步只能一个矩形"：union 是精确的（实机校验 3.4 %），K 再大也只是回到包围盒台阶（K=120 相对最细台阶 +0.00 %）。要压这个 3～4 倍，只有**每步内部做最大矩形分解**（占用栅格 + 贪心覆盖，条带宽度已知 2 mm），让一步出多个贴合轮廓的槽，用槽数换材料。这才是攻 +281 %～+351 % 的那一刀，杠杆 1 不是。
