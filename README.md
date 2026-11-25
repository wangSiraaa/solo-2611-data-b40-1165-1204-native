# slabwater — 分块标高接缝与有开闸上限的蓄水核验

纯合成离散模型，仅用于可复现的规则核验，**不用于现场水利判断**。

实现为 Python 3.11 独立库（仅标准库 + SQLite），分三层：

| 模块 | 职责 |
| --- | --- |
| `slabwater/catalog.py` | 独立、只追加的分块标高目录；批次校验、冻结切面、逐格来源 |
| `slabwater/solver.py` | 水位/蓄水计算；按出口集合搜索可行计划 |
| `slabwater/publisher.py` | 发布、幂等复读、拒绝留证、核计划、重开 |
| `slabwater/sun.py` | 无状态日照规则与健壮策略引擎；方向卡解析、遮挡与逐格来源、固定/带轮经营 |
| `slabwater/adoption.py` | 苗床认养合同与实地交接账本；签约、幂等、拒绝留证、交接、重开 |
| `slabwater/winter.py` | 确定性冬季苗床材料生成（北墙 + 东西畦） |

## 目录（Catalog）

固定 `H×W` 格面，每块给出 `tile_id`、`revision`、原点 `(row,col)`、整数矩形与高度。一次批次：

- 可同时给出多块，**按 `tile_id` 替换**；批次未提及的块沿用上一版。
- 批次整体校验，全部通过才追加一个新版本；失败则**不落任何版本**，旧产品不变。
- 校验项：矩形越界、`tile_id` 重复、同一 `(tile_id,revision)` 内容被改写（immutable）、
  切面**未完整覆盖**（`coverage_gap`）、**重合格高度不一致**（`overlap_mismatch`）、
  格高超过上限 `C`（`height_above_ceiling`）。
- 每版冻结 `heights`、`elevation_hash` 与逐格 `sources`（来自哪块、块的修订、行列局部坐标）。

历史版本可随时 `get_slice(version)` 读取，发布端据此独立取标高，互不影响。

## 水位模型（Solver）

四邻移动，整数高度。对已打开的出口集合：

1. 一格到某打开出口的**排水门槛** = 路线经过的所有格高与出口门槛 `sill` 的最大值。
2. 该格水位 = `min(C, 所有打开出口门槛的最小值)`；无任何出口可达时为 `C`。
3. 蓄水 = 水位 − 本格高（逐格一致计算）。

这不是最短路求和，而是**最小化最大瓶颈**（minimax）的 Dijkstra：扩展边时取
`max(当前瓶颈, 邻格高)`。因此**不能直接用最低出口门槛**——路径上的高堤仍会抬升水位
（见测试 `test_lowest_sill_alone_is_not_enough`）。

### 两种搜索策略

- `recompute`：对每个候选出口集合跑一次多源 minimax Dijkstra。直接、易证正确。
- `reuse`：每个出口只算一次逐格瓶颈图，集合内逐格取**最小**组合。候选集合多时更省。
  瓶颈图只对**某一冻结切面**有效。

`BottleneckCache` 以 `elevation_hash` 为键：切面一旦改变（新版本、标高修订），
下次查询立即**清空失效**并计数 `invalidations`。在 12×12、8 出口压力样例中
（最多开 8 个，共 256 个集合）：

| 策略 | Dijkstra 次数 | 耗时 | 结果 |
| --- | --- | --- | --- |
| reuse | 8（每出口一次） | 0.028s | 与 recompute 完全一致 |
| recompute | 256 | 0.119s | — |

选择建议：候选集合少、或只需核一个既定集合 → `recompute` 更直接；候选集合多且同一
切面反复查询 → `reuse`。任何标高修订后不要跨切面复用瓶颈图，交给缓存自动失效。

搜索按“集合大小升序、再按出口 id”确定性枚举，因此同输入必然得到同一产品（无需最小开闸数）。
`stop_on_first=True` 命中即返回；`False` 则遍历全部并在可行集合中取总蓄水最小者，同时保留全部尝试。

## 发布（Publisher）

请求需给候选出口、`opens`（最多开启数，未要求必须开满）、以及 `total_max` 与 `cell_max`
（若干指定格蓄水上限）。消费端只读**已冻结**的 `source_revision` 切面。

- **成功**：写正式 `plans` 行，产品含开启出口、逐格水位/蓄水、总量、可复查的
  `elevation_source`（版本、`elevation_hash`、块清单）与 `artifact_hash`。
- **拒绝**：不写正式计划；保存真实尝试的**穷尽证据**：每个候选集合的开口、总量、指定格蓄水
  与违反的约束（`total` / `cell`），以及 `exhaustive=true`。可直接下载核对。
- **错误**（未知版本、出口越界/不在边界、请求非法）：记错误结果，同样不写正式计划。
- **幂等**：同一申请 `id` + 同一输入 → 原样复读首次结果（成功返回同一 `plan_id` 与工件；
  失败返回同一证据）。同一 `id` 换输入 → `idempotency_conflict`，绝不静默覆盖。
- **版本隔离**：新标高版本不改写旧 `plans` 行；`get_plan`/`verify`/`reopen` 均按产品内
  冻结的版本与哈希核验。

### 读标高 / 核计划 / 发布 / 重开入口

```python
from slabwater import Catalog, Publisher

catalog = Catalog("catalog.db", height=1, width=5, ceiling=6)
publisher = Publisher("plans.db", catalog)

catalog.apply_batch([...tiles...])          # 追加切面版本
publisher.read_elevation(version=1)         # 读标高（含逐格来源）
publisher.publish(request, default_outlets=[...])  # 实际发布
publisher.verify(plan_id)                   # 核计划（重算并重搜索）
publisher.reopen(plan_id)                   # 重开已存工件
```

命令行：

```bash
python3 -m slabwater run materials/772/input.json --db-dir /tmp/run772
python3 -m slabwater verify /tmp/run772 <plan_id>
python3 -m slabwater reopen /tmp/run772 <plan_id>
```

## 样例核对（materials/772）

- 初版 `[0,4,1,4,0]`，双口全开：水位 `[1,4,4,4,3]`，蓄水 `[1,0,3,0,3]`，总 7，中格 3。
  A1（总量≤7、中格≤2）必拒：没有任何集合同时满足，证据穷尽列出（含双开的 `cell` 违例）。
- 降右侧堤后 `[0,4,1,2,0]`，双口全开：水位 `[1,4,3,3,3]`，蓄水 `[1,0,2,1,3]`，总 7、中格 2。
  A2（≤7、中格≤2）成功，开启 `L,R`。
- A3 只允许开 1 口：`L` 总 10、`R` 总 10，均超总量 7（`R` 虽满足中格≤2），故拒绝。
- `invalid_correction` 使重合格 `(0,2)` 出现 1 与 2，冲突 → 整版拒绝，版本仍为 `[1,2]`。

## 冬季苗床认养（Sun / Adoption）

冬季只出租**能连晒约定拍数**的苗床。输入是已发布的地形切面号、三到四拍的太阳方向卡与菜畦位置。
方向卡写明到某畦的沿线格与每格射线高度；中途格**标高达到射线**（`elevation >= ray_height`）
即遮住该拍，标高一律从**真实冻结版本**解析，并随遮挡结果记下来源块（块 id、修订、局部行列）。

### 日照与天气规则

- 天气表给出若干**整日可能**，每种可能逐拍给出晴/阴，且同一种天气对所有畦同时生效——
  不能在甲畦挑一天的晴时、又给乙畦换另一天。
- 认养者要的是**同一盘苗在同一天真正连续照足约定拍数**；两盘各晒一拍不能合算，逐盘独立计连胜。
- 一条入射线可经过多个沿线格，任一格达到射线即遮挡；遮挡证据逐块保留可复查来源。

### 两种经营（签约时二选一）

- **固定种植畦（`mode=fixed`）**：开单即划定成片床位，一盘苗绑定一畦，下地后不能因阴影改投隔壁。
  只需证明该畦在**每一种允许天气**下都能凑足约定连胜，否则不签。
- **带轮苗床（`mode=mobile`）**：可按当天**已观测到**的天气，把同一盘沿申报通道推到有使用权的另一畦。
  移动需要空通道与当班园丁，**移动本身耗一拍且该拍不记日照**；策略在任何分支都不得读取未来拍的天气。

策略引擎对天气不确定性做非预期（AND/OR）健壮搜索：

- `reveal=dawn`：天气类型在首拍早晨揭晓，第 1 拍动作即可按天气分支；
- `reveal=progressive`：只能依据已观测拍决策，第 1 拍动作被所有尚未区分的天气共用。

限制：最多 **4 畦、4 拍、2 盘**。签约时对每种允许天气都能如约才签；不能兑现则在拒绝证据中
指出**哪一盘在何拍**丢了连续日照（`shaded` / `cloudy` / `move_beat`）及对应遮挡来源块。

### 实地交接（Hand-over）

交接在签约之后的某一真实日子执行，另给当天 `weather_id`、当班 `gardeners`、被其他客户占用的
`occupied` 床位，以及可选的 `observed_revision`（在更新的实测地形上核验）。在线逐拍：

- 仅在仍与观测相容的天气可能中选健壮动作；目标床位被占、通道缺失、园丁不足都会留下
  `bed_occupied` / `no_corridor` / `no_gardener` 证据；
- 合同冻结签约时的版本与 `elevation_hash`，新地形不改写旧合同；交接报告同时保留
  “签约地形”与“实测地形”两份哈希与 `terrain_changed` 标记；
- 每盘每拍（含移动拍、遮挡来源块）都落账，供售后读回。

### 签约 / 交接 / 重开入口

```python
from slabwater import Catalog, AdoptionRegistry

catalog = Catalog("catalog.db", height=1, width=4, ceiling=6)
catalog.apply_batch([...tiles...])
registry = AdoptionRegistry("adoption.db", catalog)

result = registry.sign(request)                 # 健壮可履约才签，否则 rejected/error + 证据
registry.handover(handover_request)             # 当天实地交接，逐拍日照账本
registry.get_contract(contract_id)              # 读回合同（畦权、策略、遮挡表）
registry.get_handover(handover_id)              # 读回某次交接的逐拍记录
registry.verify(contract_id)                    # 在冻结地形上重算健壮策略并核对工件哈希
registry.reopen(contract_id)                    # 重开：取回当时畦权与日照记录
```

命令行：

```bash
python3 -m slabwater adopt <db_dir> sign_request.json
python3 -m slabwater handover <db_dir> handover_request.json
python3 -m slabwater contract <db_dir> <contract_id>
```

签约与交接都带幂等：同一 `id` + 同一输入原样复读；同一 `id` 换输入报 `idempotency_conflict`。
被拒签约不写合同行。样例见 `materials/winter-garden`（含北墙升高、固定畦被占、带轮苗床移动、
园丁/通道不足等全套叙事）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：切面/接缝/覆盖/越界/不可变修订/原子批次/重开；水位规则与“最低门槛不充分”；
reuse 与 recompute 一致；修订使缓存失效；A1/A2/A3；幂等复读与冲突；版本隔离；
核计划能发现篡改；12×12、8 出口压力与穷尽拒绝证据。

苗床认养（`tests/test_adoption.py`，全套 56 项中的 31 项）覆盖：射线“达到即遮挡”与来源块留证；
北墙升高由签约变拒绝并逐拍点名；固定畦床位被划走且无替畦不能交接；带轮苗床按揭晓天气移动、
移动拍不记日照、东上午/西下午连照；园丁为零、通道缺失、目标畦被占的拒绝与交接失败；
`dawn` 与 `progressive` 两种揭晓的非预期策略；整日天气不可跨畦挑天；两盘不得合算；
4 畦/4 拍/2 盘上限；签约与交接幂等及冲突；版本隔离、重开取回畦权与日照账本、篡改核验。
