# 吸塑托盘腔体 UDF 资格化流程（blister_tray_cavity_v0）

> 目的：把"Agent 生成医用吸塑盒粗坯"落到**人工可以接手编辑**的生成路线上。
> 状态：**候选清单已建、`.gph` 资产待生成**（第 1 步之前无法完成）。本文件就是给这一步的操作工单。
> 配套：清单 [blister-tray.candidate.udf.json](blister-tray.candidate.udf.json)，通用规则见
> [`creo_mcp/docs/UDF_MANIFEST.zh-CN.md`](../creo_mcp/docs/UDF_MANIFEST.zh-CN.md)。

---

## 为什么是 UDF，而不是继续用 BREP 导入

实测证据（本机 Creo 13.4.1.0，活动模型 `zhengguan_box_r01.prt`）：

```text
features: n=2  ['ZHENGGUAN_BOX_R01', 'GENERATED_SOLID']
parameters: PTC_VAL_IMP_PART_STATUS / PTC_VAL_IMP_NUMBER_OF_SOLIDS / PTC_MP_VAL_IMP_VOLUME ...
```

整个模型只有 **1 个导入的生成实体**，没有任何可"编辑定义"的参数化特征。这正是
[README.md:164](../README.md) 声明的 `CONCEPT_ONLY` + `imported_brep` 形态：**能看、能量、
能演示放不放得进去，但工程师接手时无法改特征**，只能重画。

Creo 13.4 的 VB API（以及同源的 J-Link）不暴露 extrude/revolve/shell 的原生特征创建工厂，
这条边界已经写进 `creo_get_capabilities` 的 `feature_creation`。而 **UDF 组里可以携带原生特征**
——拉伸、切除、倒圆角、拔模都能被打包进 `.gph`。所以：

- 现阶段要"有棱有角的粗坯 + 人工可精修" → 用 UDF 放置，不用 BREP 导入。
- 将来要自动化拔模 → **同一个模板加上 draft 特征后再导一版 `.gph`** 即可，
  不需要为了这件事去上 Pro/TOOLKIT / C++ DLL。

---

## 操作工单

### 第 0 步：手工做腔体模板（在 Creo 里，唯一必须人工的一步）

在 mm 单位的零件模板中，用参数化特征做出腔体，并满足：

| 要求 | 说明 |
|---|---|
| 全部尺寸命名 | 腔长、腔宽、腔深、壁厚、封边宽各起一个稳定符号名（不要用 `d11` 这类自动名交付） |
| 基准坐标系 | 一个作为腔体原点的坐标系，UDF 引用提示词将绑到它 |
| 封边平面 | 一个作为封边/分型面的基准面，作为第二个引用 |
| 内腔为单一拉伸+切除链 | 不要混入导入几何；阶梯分段可以用多个切除特征叠加，便于人工调 |
| 暂不加拔模、暂不加圆角 | 粗模阶段保持锐角（拔模留给人工，或第二版模板） |
| 不保存为业务版本 | 模板文件本身不进生产库，只作为 `.gph` 来源 |

### 第 1 步：导出 `.gph`

把模板中构成腔体的特征建成组，导出为几何特征（.gph），文件放到：

```text
<repo>\live-validation\udf\blister_tray_cavity_v0.gph
```

导出时**不要**把外部装配引用、方向回答、象限点作为依赖——清单只支持
`feature / surface / edge / coordinate_system / axis / point` 六类引用。

### 第 2 步：把真实哈希和真实尺寸名写进清单

```powershell
cd <repo>
(Get-FileHash .\live-validation\udf\blister_tray_cavity_v0.gph -Algorithm SHA256).Hash
```

用输出替换清单里的 64 个 `0`。再把清单中 5 个 `TODO_*_DIM` 换成真实尺寸符号名与上下限：
先用 `creo_get_model_structure`（或 `creo_inspect_part_dimensions`）读取模板的驱动尺寸，
不要凭记忆填。`references[].prompt` 必须与导出时 Creo 实际的提示词逐字一致。

### 第 3 步：离线校验（不启动 Creo，安全）

```powershell
.\.venv\Scripts\creo-mcp.exe --operation validate_udf_manifest `
  --payload '{""manifest_path"":""<repo>\\live-validation\\blister-tray.candidate.udf.json""}'
```

或用 MCP 工具 `creo_validate_udf_manifest`。当前实测返回：

```text
error: Qualified UDF file does not exist: ...\live-validation\udf\blister_tray_cavity_v0.gph
```

说明**Schema 已通过**，只差资产。同一命令对 PTC 自带样例 `node.candidate.udf.json` 返回 `ok=true`
（size 9786 B，hash 匹配），可作为流程自检基线。

### 第 4 步：受控真实资格验证

前置：确认 broker 正常（`--doctor --probe`），目标是一个**干净的测试模型**，不是你正在用的生产件。

1. 记录目标模型类型与单位；
2. `creo_get_model_structure` 取得真实引用 ID；
3. `creo_run_udf_qualification` 传入清单声明的**全部**引用与变量尺寸（此时
   `verified_non_interactive` 必须仍为 `false`）；
4. 全程不允许出现引用/方向/象限/尺寸/修复/重定义任何弹窗——出现即判失败；
5. 校验返回：组成员完整、重生成成功、腔体尺寸符合预期；
6. 对每个尺寸上下限各测一次，再测一个非法值：非法值必须在调用 Creo 前被拒绝；
7. 全部通过后，人工把 `verified_non_interactive` 改为 `true`（这是工程资格声明，工具不会自动改），
   并对最终 `.gph` 重新计算 SHA-256 写入清单。

失败处理：Creo 可能已经创建部分特征，MCP 不做事务回滚。**不要自动重试**，先检查当前模型状态。

### 第 5 步：Skill 层的调用序列（业务在这里，不在 MCP）

```text
读器件包围盒 → 按工艺规则算腔体参数 → creo_place_qualified_udf → 验证 → 交人工
```

Skill 必须遵守：

- **参数白名单**：只允许腔长/腔宽/腔深/壁厚/封边宽 5 个数值，且受清单 `variant_dimensions` 上下限约束；
  不接受任意路径、不接受草图、不接受自由坐标。
- **余量策略写死在 Skill**：余量、壁厚、封边的数值来源必须是**厂内标准**。网上流传的
  拔模 1–3°/3–5°、封边 8–12 mm、R≥4–5× 料厚属于经验区间，来源互相矛盾，不能直接当交付依据。
- **输出要披露假设**：返回"本次用了什么余量/壁厚/腔体尺寸 + 待人工处理清单"（待拔模面、
  待倒圆角内直角、阶梯垂直过渡面优先区）。不披露的粗模就是黑盒。
- **门禁复用已有校验**：放入可行性用 `creo_plan_product_clearance_tray` 的完整包含 + `+Z` 直插检查，
  不要用文件级包围盒代替。

---

## 拔模与阶梯面的边界（现在就写清楚，避免以后返工）

1. 粗模阶段**不做拔模**：VB API 无法创建原生 draft 特征；第二版模板把 draft 打进 UDF 才是自动化的正解。
2. 阶梯块方向正确（平面最容易拔），但**阶梯垂直过渡面是倒扣高发区**：吸塑件不允许倒扣，
   这些面必须出现在待办清单里，由人工优先处理。
3. Creo 的拔模无法作用于已有圆角边界，正确顺序是**先拔模再倒圆角**；因此粗模保留锐角是对的。
4. "让人工接手编辑"是这条流程的验收标准：打开模型必须能看到**可展开的 UDF 组和可改的尺寸**，
   而不是一个孤立的导入实体。若验收时只看到 `GENERATED_SOLID`，说明又走回 BREP 路线了。
