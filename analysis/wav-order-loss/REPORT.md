# WAV 处理顺序与不可逆细节丢失：实测分析

本目录是一次面向**声音编辑**的实测分析，目标是判断现有 WAV 配方中
增益、混音、归一化、裁剪的不同合法顺序，哪些会让安静细节或瞬态在成片中
**不可逆地**丢失。所有媒体都由仓库内现有服务（`avprovenance`，未改动）
生成；结论来自真实输出字节的采样，而不是对公式的推演。

- 分析目录：`analysis/wav-order-loss/`
- 原项目代码：**未改动**（`git diff` 为空；`analysis/` 是唯一新增）
- 每个实验进程：独立进程，`RLIMIT_AS = 192 MiB`
- 实测规模：33 个 trial，其中 23 个完成、10 个因内存预算未完成（全部保留）

---

## 1. 结论

**用最终峰值判断两个配方是否等价是错的。** 同一份素材、不同的合法顺序，
可以做到最终峰值完全相同（左声道都是 16384），却出现 12.9 dB 与 90.6 dB
两种信噪比——前者是明显可听的失真，后者是干净结果。最终峰值相同只说明
最后一个 `audio_normalize` 把峰值拉到了同一目标，它不说明中间是否已经
削平或量化掉了内容。

按丢失机制归类，实测得到以下结论（每条都对应下面的真实采样证据）：

| 机制 | 触发顺序 | 丢失内容 | 可逆性 |
| --- | --- | --- | --- |
| 中间削波 | 先 `+6 dB`（或先混音求和）再 `normalize` | 峰值段被硬削平 | **不可逆**，后续归一化只缩放已削平的波形 |
| 安静通道量化归零 | 深衰减（如 `-48/-24 dB`）后再放大回原电平 | 低于 1 LSB 的细节被舍入为 0 | **不可逆**，放大 0 仍是 0 |
| 混音求和溢出 | 先 `audio_mix` 再 `normalize` | 两源之和超过满量程的部分被削 | **不可逆** |
| 归一化参照点错位 | 先 `normalize`（按整段峰值）再 `trim` | 目标窗口整体电平被压低（实测 16.7 倍） | 不可逆地改变了所选窗口的电平 |

**同等峰值不等于同等声音的实例**（左声道，全部最终峰值 16384）：

| 顺序 | 最终峰值 | 与干净参考的 SNR | 判定 |
| --- | --- | --- | --- |
| `normalize` | 16384 | 参考本身 | 干净 |
| `-6dB → normalize` | 16384 | 90.6 dB | 干净（数值舍入） |
| `+6dB → normalize` | 16384 | **12.9 dB** | 削波失真 |
| `normalize → +12dB → normalize` | 16384 | **12.7 dB** | 削波失真 |
| `-6dB → +6dB → normalize` | 16384 | 88.8 dB | 仅双重舍入 |

**单侧损失不能用左右平均掩盖。** 在安静通道实验里，右声道（1500 Hz 导频，
峰值 30）被深衰减再放大后变成全零；而把成品做 `(L+R)/2` 单声道折叠后，
信噪比仍有 71.6 dB，听起来几乎无损——右声道的 14.6 dB 严重损伤被响亮的
左声道稀释掉了。本报告所有失真指标都**按声道分别给出**，从不先做左右平均。

---

## 2. 被测服务与合法顺序

服务来自仓库现有代码，未做任何修改：

- 音频操作实现：`avprovenance/media/audio_ops.py`
- 顺序执行器：`avprovenance/media/audio_ops.py::render_audio`
- 计划校验：`avprovenance/renditions/plan.py`
- 上传上限：`avprovenance/validation.py`（解码后 24 MiB）

关键实现事实（由源码确认，并用于解释实测）：

1. **每个操作都落回 int16 并硬削波。** `_clip_sample()` 在任何一次
   `gain`、`mix`、`normalize` 之后都会把样本夹到 `[-32768, 32767]`。
2. **归一化按当前整段峰值缩放。** `normalize_audio()` 取整段（所有声道）
   的绝对峰值作为分母；先 `trim` 后 `normalize` 与先 `normalize` 后
   `trim` 因此参照点不同。
3. **混音是“先求和再削波”。** `mix_audio()` 逐样本相加后立即 `_clip_sample`。
4. **这些顺序全部是合法计划。** 计划校验只检查参数范围与 `audio_mix`
   是否带次源，不限制 gain/normalize/mix/trim 的先后。本实验的 33 个计划
   全部通过服务的 `compile_plan`（已单独校验，0 个非法）。

---

## 3. 实测素材

素材由 `tools/materials.py` 生成，全部是服务接受的 2 声道 PCM s16le。
“峰值相同”由**实际写出的字节**验证（`tools/verify_materials.py`）。

### 3.1 相同峰值、不同能量分布的双声道素材

| 素材 | 左声道内容 | 文件峰值 | 左声道 RMS | 左声道波峰因数 | 左声道静音比例 |
| --- | --- | --- | --- | --- | --- |
| `dense_stereo_2s.wav` | 连续 440 Hz 正弦 | **32000** | 22627.4 | 1.41 | 1.0% |
| `sparse_transient_stereo_2s.wav` | 每秒 4 个 25 ms 瞬态 | **32000** | 5148.2 | 6.22 | 90.1% |

两者文件峰值完全相等（32000），但 RMS 相差 4.4 倍、波峰因数相差 4.4 倍。
两个素材的右声道相同：1500 Hz 导频，峰值 30（相对左声道约 -60 dB），
用于观察“安静通道是否先丢”。

左/右声道不是彼此的近似：`dense` 的左右 RMS 比为 1073.9，
`sparse` 为 244.3，采样差异比例分别为 99.5% / 88.7%。因此单看一个声道
不能代表另一个（见 `out/runs/analysis.json` 的 `channel_distinctness`）。

### 3.2 强弱不等的两源混音素材

| 素材 | 内容 | 峰值 |
| --- | --- | --- |
| `mix_strong_primary.wav` | 左 440 Hz / 右 587 Hz | 30000 |
| `mix_weak_secondary.wav` | 左 880 Hz / 右 1046 Hz | 6000（约为强源 -14 dB） |
| `mix_quiet_secondary.wav` | 左 880 Hz / 右 1046 Hz | 600（约 -34 dB） |

强源单独已接近满量程；与次源直接相加会超过 32767，这正是“先混音后归一化”
会削波的来源。

### 3.3 接近上传上限的输入

`inputs/near_limit_dense.wav` 与 `inputs/near_limit_sparse.wav` 各
**25 000 000 字节**，占 24 MiB 解码上限的 **99.34%**，两份都使用了不同
能量分布（dense/sparse），满足“至少两份各接近当前上传上限”的要求。

---

## 4. 顺序族实测结果

所有数值来自服务真实输出的 WAV 文件，按声道分别比较。比较方法：把待测输出
与它声明的参考输出按最小二乘对齐电平后求残差，得到逐声道 SNR 与差异采样
比例（`tools/analyze.py`）。对齐电平是为了不把“整体电平不同”误判为失真。

### 4.1 F1 增益 / 归一化顺序（dense 与 sparse 各一组）

参考为单次 `normalize(target_peak=0.5)`，所有计划最终左声道峰值都是 16384。

| Trial | 顺序 | 左峰值 | 左 SNR | 左差异比例 | 右峰值 | 中途削波步 | 分类 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `f1_dense_norm_only` | `N` | 16384 | 参考 | — | 15 | — | 干净 |
| `f1_dense_atten_then_norm` | `G-6 → N` | 16384 | 90.6 dB | 12.0% | 15 | 无 | 干净 |
| `f1_dense_clip_early` | `G+6 → N` | 16384 | **12.9 dB** | 99.0% | 30 | 第 1 步 | 削波 |
| `f1_dense_clip_late` | `N → G+12 → N` | 16384 | **12.7 dB** | 99.0% | 30 | 第 2 步 | 削波 |
| `f1_dense_net_zero` | `G-6 → G+6 → N` | 16384 | 88.8 dB | 18.0% | 15 | 无 | 仅舍入 |
| `f1_sparse_atten_then_norm` | `G-6 → N` | 16384 | 83.5 dB | 3.1% | 15 | 无 | 干净 |
| `f1_sparse_clip_early` | `G+6 → N` | 16384 | **12.4 dB** | 9.9% | 30 | 第 1 步 | 削波 |
| `f1_sparse_clip_late` | `N → G+12 → N` | 16384 | **12.2 dB** | 9.9% | 30 | 第 2 步 | 削波 |
| `f1_sparse_net_zero` | `G-6 → G+6 → N` | 16384 | 83.7 dB | 3.0% | 15 | 无 | 仅舍入 |

要点：

- 削波发生在**中间某一步**，随后 `normalize` 把峰值拉回 16384。最终峰值
  完全无法区分干净与失真。
- sparse 的差异采样比例只有 9.9%，但 SNR 仍低至 12.2 dB：丢失集中在
  **瞬态**上，听感上正是“瞬态被压扁”。这正是“安静细节或瞬态”里的瞬态情形。
- `net_zero`（`-6` 再 `+6`）证明纯量化往返只带来约 89 dB 的微小误差；
  真正致命的是**在错误的位置越过了满量程**，而不是“多做了一步”。

逐样本削波证据（来自 `out/runs/step_trace.json`，直接用服务的
`gain_audio`/`normalize_audio` 复算中间态）：

- `f1_dense_clip_early` 第 1 步 `+6 dB`：左声道 10400 个样本被削到满量程，
  峰值 32768；第 2 步归一化后满量程样本数回到 0——**削波被归一化“藏起来”了**。
- `f1_sparse_clip_late` 第 2 步 `+12 dB`：592 个样本被削平；第 3 步归一化后
  又是 0 个满量程样本。

### 4.2 F2 安静通道量化（dense，右声道导频峰值 30）

参考仍为单次归一化。

| Trial | 顺序 | 右峰值 | 右 SNR | 右差异比例 | 右零样本（参考/实测） | 分类 |
| --- | --- | --- | --- | --- | --- | --- |
| `f2_dense_norm_only` | `N` | 15 | 参考 | — | 2000 / 2000 | 干净 |
| `f2_dense_quiet_2lsb` | `G-24 → G+24 → N` | 16 | **14.6 dB** | 87.5% | 2000 / 2000 | 安静细节粗糙化 |
| `f2_dense_quiet_annihilated` | `G-48,-24,+24,+24,+24 → N` | **0** | **—**（无信号） | — | 2000 / **16000** | 安静细节被抹除 |

要点：

- 右声道先衰减 72 dB 再放大回来，峰值从 15 变 0，整条导频变成数字静音。
  左声道同时段仍有 16384 峰值。**单侧损失真实存在且完全不可逆。**
- 同一份 `quiet_2lsb` 输出里，左声道 SNR 是 74.5 dB（几乎无损），右声道是
  14.6 dB。如果做 `(L+R)/2` 折叠，合并 SNR 是 **71.6 dB**——把右声道的
  严重损失几乎完全掩盖。这直接说明为什么报告拒绝左右平均。

### 4.3 F3 强弱两源混音顺序

| Trial | 参考 | 左峰值（参考/实测） | 左 SNR | 右峰值（参考/实测） | 右 SNR | 中途削波步 | 分类 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `f3_weak_norm_first` | 自身 | 14239 | 参考 | 16383 | 参考 | 无 | 干净 |
| `f3_weak_mix_first` | norm_first | 14239 / 16029 | **16.2 dB** | 16383 / 16384 | **15.8 dB** | 第 1 步 | 混音削波 |
| `f3_weak_balanced` | 自身 | 14297 | 参考 | 16384 | 参考 | 无 | 干净 |
| `f3_weak_gain_up` | balanced | 14297 / 16384 | **3.1 dB** | 16384 / 16384 | **3.2 dB** | 第 1、2 步 | 混音削波 |
| `f3_quiet_norm_first` | 自身 | 15847 | 参考 | 16384 | 参考 | 无 | 干净 |
| `f3_quiet_mix_first` | norm_first | 15847 / 16076 | 35.6 dB | 16384 / 16384 | 35.6 dB | 无 | 干净（未溢出） |
| `f3_quiet_boost_secondary` | norm_first | 15847 / 16384 | **13.4 dB** | 16384 / 16384 | **13.4 dB** | 第 1、2 步 | 混音削波 |

要点：

- `f3_weak_mix_first` 的**最终峰值**与参考非常接近（左 16029 对 14239，
  右 16384 对 16383，后者几乎相同），但 SNR 只有 16 dB。又一次证明
  峰值相同 ≠ 声音等价。
- 削波证据：`f3_weak_mix_first` 第 1 步混音后右声道 1280 个样本被削平；
  `f3_weak_gain_up` 第 1 步 `+6 dB` 削掉约 1 万个样本，第 2 步混音再削约
  5 千个，归一化后满量程样本数都为 0。
- `f3_quiet_mix_first` 是**反例对照**：次源足够安静（600 峰值）时，先混音
  也没有溢出，35.6 dB、无中途削波。说明“先混音后归一化”本身不必然坏，
  坏的是**让求和越过满量程**。这个结论依赖两源的实际电平，不能一概而论。

### 4.4 F4 裁剪与归一化顺序（电平参照点错位）

素材 `dynamics_stereo_2s.wav`：左声道是约 1910 幅度的持续音，叠加每秒一次
的满量程瞬态；窗口取 0.45–0.55 s（不含瞬态）。

| Trial | 顺序 | 左峰值 | 左 SNR | 右峰值 | 分类 |
| --- | --- | --- | --- | --- | --- |
| `f4_trim_then_norm` | `T → N` | **16384** | 参考 | 257 | 目标窗口正常归一化 |
| `f4_norm_then_trim` | `N → T` | **978** | 68.0 dB | 15 | 窗口电平被压低 16.7 倍 |

要点：

- 两种顺序都“合法”，最终文件都是该窗口；但先归一化会按**整段峰值**
  （32000 的瞬态）计算系数，等到裁掉瞬态时，留下的安静窗口只剩 978 峰值。
  SNR 68 dB 说明波形形状没坏（可逆缩放），但**所选窗口的电平被不可逆地
  改小了 16.7 倍**（约 -24.5 dB）。作为“声音编辑”结果，这等于把安静段
  做成了几乎听不见。

---

## 5. 192 MiB 独立进程预算的实测边界

每个 trial 都在**独立子进程**里运行，子进程启动服务前设置
`RLIMIT_AS = 192 MiB`；峰值内存从 `/proc/self/status` 的 `VmHWM` 读取。

### 5.1 渲染内存随输入近似线性增长（约 34–40 倍输入字节）

在预算外单独测量真实渲染路径（`execute_plan`，仅用于刻画边界）：

| 输入字节 | 进程峰值 RSS | 相对基线增量 |
| --- | --- | --- |
| 1 000 000 | 55.4 MiB | 33.5 MiB |
| 2 000 000 | 88.7 MiB | 65.8 MiB |
| 3 000 000 | 122.8 MiB | 99.0 MiB |
| 4 000 000 | 160.9 MiB | 136.2 MiB |
| 9 600 044 | **427.1 MiB** | 397.0 MiB |

原因是 `decode_wav()` 把 PCM 解成 Python `int` 列表（每样本约 28 字节），
一次计划里还会同时存在多个中间副本。因此**接近上传上限的输入无法在
192 MiB 内渲染**——这不是配置问题，是当前实现的内存特征。

### 5.2 预算阶梯（同一合法计划，输入 1–20 MB）

下表的“完成/未完成”来自主实验那一次运行；边界稳定性另见 5.2.1。

| 输入 | 主实验结果 | 到达阶段 | 归因 | 峰值 RSS* |
| --- | --- | --- | --- | --- |
| 1 MB | **完成** | 媒体已保存 | — | 59.8 MiB |
| 2 MB | 未完成（**边界抖动**，见 5.2.1） | rendition_failed | `MemoryError @ execute_plan` | 102.5 MiB |
| 3 MB | 未完成 | rendition_failed | `MemoryError @ execute_plan` | 102.4 MiB |
| 4 MB | 未完成 | rendition_failed | `MemoryError @ execute_plan` | 104.7 MiB |
| 8 MB | 未完成 | rendition_failed | `MemoryError @ execute_plan` | 96.9 MiB |
| 16 MB | 未完成 | rendition_failed | `MemoryError @ execute_plan` | 83.3 MiB |
| 20 MB | 未完成 | rendition_failed | `MemoryError @ execute_plan` | 98.6 MiB |

\* 失败点峰值 RSS 随堆布局抖动，同一档位在不同运行中可差十几 MiB；
它只说明“撞到了 192 MiB 墙”，不是稳定的档位特征值。

注意：主实验里 20 MB 的**上传**成功了（201），失败发生在渲染阶段；而在
5.3 的独立探测中，20 MB 上传多次失败。两者不矛盾——上传服务上限本身也是
抖动带（见 5.3）。

#### 5.2.1 边界不是一条干净的分割线

2 MB 恰好落在 192 MiB 边界上，会随 Python 堆布局抖动。重复实验
（`out/runs/boundary_stability.json`）：

- 1 MB：8/8 完成（稳定）；
- 2 MB：记录批次 0/8，三次补充批次 3/10、1/12、0/6，合计 **4/36 完成**；
- 3 MB、4 MB、8 MB、16 MB：每档 0/8 完成（稳定失败）。

因此更准确的说法是：**1 MB 稳定可完成，约 2 MB 是窄抖动带，3 MB 及以上
稳定失败**。主实验把 `f5_budget_2mb` 记为未完成，是因为那一次确实失败了；
这里同时给出抖动率，避免把边界说成精确阈值。

注：服务把渲染期 `MemoryError` 包装成了稳定的 `RENDER_FAILED`（HTTP 409），
上传期 `MemoryError` 则是 `INTERNAL_ERROR`（HTTP 500）。为了不让“为什么
没完成”从证据里消失，子进程以**只观测、不改行为**的方式包裹了
`execute_plan`、`apply_media_operations`、`read_json_body`、
`decode_media_payload`、`ingest_asset`，记录逃逸的异常类型后原样重新抛出。
这些观测记录写在每个 trial 的 `render_exceptions_observed` 字段里。

### 5.3 上传上限的实测天花板

服务对外声明解码后上限 24 MiB（`MAX_MEDIA_BYTES`）。但在 192 MiB 独立进程
内，**上传本身**会先撞到内存墙，而且这个墙是**抖动的带**，不是干净阈值。
测量方法：每个候选尺寸都启动一个**全新的** `RLIMIT_AS=192 MiB` 进程，
每个进程只做一次流式上传；每个尺寸重复多次。

| 候选字节 | 接受次数 / 尝试次数 |
| --- | --- |
| 15 000 000 | 12 / 12 |
| 16 000 000 | 12 / 12（另有 6/6 复核） |
| 17 000 000 | 3 / 12 |
| 18 000 000 | 1 / 12 |
| 19 000 000 | 1 / 12 |
| 20 000 000 | 0 / 12 |
| 21 000 000 | 3 / 12 |
| 22 000 000 | 0 / 12 |
| 23 000 000 | 0 / 12 |
| 24 000 000 | 0 / 12 |
| 25 000 000 | 0 / 12（另有 0/6 复核） |

结论：在当前实现 + 192 MiB 预算下，**16 MB 稳定可上传，25 MB 稳定不可上传，
中间 17–21 MB 是抖动带**（取决于当次堆布局与峰值时序）。因此实际可用上限
**明显低于**文档的 24 MiB。

方法上的一个坑值得记下：如果在一个长寿命进程里连续上传多个候选，已释放的
内存不一定还给 `RLIMIT_AS`，会测出偏低的“上限”。本文件的数字全部来自
每个尺寸一个全新进程，因此不受该效应影响。

`read_json_body` 先整体读出请求体、再 `json.loads` 整个 base64 字符串、
再 `b64decode`，峰值约为请求体的数倍；这就是 24 MiB 上限在 192 MiB 进程里
达不到的直接原因。

### 5.4 两份接近上限的输入：保留为未完成证据

| Trial | 输入 | 结果 | 到达阶段 | 归因 |
| --- | --- | --- | --- | --- |
| `f6_near_dense_reference` | 25 000 000 B dense | 未完成 | upload_primary | `MemoryError @ read_json_body` |
| `f6_near_dense_clip` | 25 000 000 B dense | 未完成 | upload_primary | `MemoryError @ read_json_body` |
| `f6_near_sparse_reference` | 25 000 000 B sparse | 未完成 | upload_primary | `MemoryError @ read_json_body` |
| `f6_near_sparse_clip` | 25 000 000 B sparse | 未完成 | upload_primary | `MemoryError @ read_json_body` |

这两份 99.34% 上限的输入**都尝试了两种顺序**，但因为上传阶段先触发
`MemoryError`，没有任何渲染发生。它们作为“预算无法完成的组合”保留在
`out/runs/raw_results.json` 与报告清单中，**没有从结论里消失**。

需要明确区分两件事：

- 本报告**没有**声称这两个顺序在声音上等价或不等价——它们根本没跑到渲染。
- 本报告**也没有**声称大输入会丢失更多细节——请勿外推。它们只证明
  “当前实现 + 192 MiB 无法处理接近上限的输入”。

---

## 6. 可试听对照

`out/audition/` 下的 WAV **全部是服务真实输出的逐字节拷贝**（已校验 SHA-256
与 trial 记录一致），没有重新合成。每个对照集包含：

- `reference.wav` / `lossy.wav`：两组真实成片原样；
- `reference_channelN.wav` / `lossy_channelN.wav`：把关注声道单独导出，避免
  被另一声道掩蔽；
- `lossy_mono_folddown.wav`：`(L+R)/2` 单声道折叠，用来**演示**平均会掩盖
  单侧损失；
- `difference_plus24dB.wav`：待测通道与参考的逐样本差，放大 24 dB，
  让差异可听可看。

| 对照集 | 参考 → 待测 | 关注 | 听什么 | 实测 |
| --- | --- | --- | --- | --- |
| `F1_dense_clip_vs_reference` | `norm_only` → `clip_early` | 左 | 同峰值下，削波版变成方波般的嗡声 | 12.9 dB |
| `F1_sparse_early_vs_late_clip` | `atten_then_norm` → `clip_late` | 左 | 瞬态被压扁 | 12.2 dB |
| `F2_quiet_channel_annihilated` | `norm_only` → `quiet_annihilated` | 右 | 右声道整段变静音 | 参考 15 → 0 |
| `F3_mix_clip_vs_norm_first` | `norm_first` → `mix_first` | 右 | 同峰值，混音先做导致失真 | 15.8 dB |
| `F4_trim_order_level` | `trim_then_norm` → `norm_then_trim` | 左 | 同一窗口电平低 16.7 倍（-24.5 dB） | 16384 → 978 |

清单见 `out/audition/audition_index.json`。

---

## 7. 证据与产物索引

| 文件 | 内容 |
| --- | --- |
| `out/runs/raw_results.json` | 33 个 trial 的原始记录（含未完成项的阶段与归因） |
| `out/runs/analysis.json` | 逐声道指标、15 组逐声道比较、单声道折叠对照、声道差异性 |
| `out/runs/step_trace.json` | 22 个 trial 的逐步中间态削波证据 |
| `out/runs/materials_evidence.json` | 素材“峰值相同、能量不同”的字节级验证 |
| `out/runs/upload_ceiling.json` | 192 MiB 内上传天花板的实测 |
| `out/runs/boundary_stability.json` | 2 MB 边界抖动率的重复实验 |
| `out/runs/manifest.json` | 媒体文件 ↔ 实际配方 的配对清单（含完整性核对） |
| `out/runs/experiment.log` | 运行日志 |
| `out/media/*.wav` | 服务真实输出（23 个完成项） |
| `out/media/*.recipe.json` | 与每个 WAV 同名的配方边车文件 |
| `out/audition/` | 可试听对照 |

### 7.1 媒体与配方的对应关系在“部分失败 + 重新整理资料”后仍然成立

`out/runs/manifest.json` 用**两个独立来源**重建配对并交叉核对：

1. 捕获时写的边车 `out/media/<trial>.recipe.json`（rendition id、plan digest、
   归一化操作、输出摘要）；
2. 原始 trial 记录。

核对结果：23 个完成的媒体全部通过（媒体 SHA-256 = 记录摘要，
边车 plan digest / 归一化操作 / rendition id 全部一致），**0 处不一致**。

我还做了一次“重新整理”演练：把 23 个 WAV 和边车移动到嵌套目录后重新配对，
23/23 仍然一一对应、0 失配。原因是配方跟着媒体文件走（同名边车），
不依赖目录结构或运行顺序。

一个值得注意的细节：`f1_dense_norm_only` 与 `f2_dense_norm_only` 是两个
不同的 rendition（不同 id、不同计划上下文），但计划相同、输入相同，因此
**输出字节相同**（共享同一个内容寻址 blob）。清单按 rendition id 和
plan digest 保留了两条独立记录，没有因为字节相同就把它们合并——配方归属
仍然各自正确。

---

## 8. 局限

- **未完成即未完成**：F5 的 2–20 MB 与 F6 的两份近上限输入没有产出成片。
  报告不推断它们的声音结果。
- **声音结论基于合成信号**：正弦、瞬态串与导频便于精确定量，但不能替代
  真实节目素材的主观评价。可试听对照正是为此保留。
- **SNR 是在最小二乘电平对齐之后计算的**，用于区分“整体电平不同”与
  “波形被破坏”。F4 是电平参照点问题，其 68 dB 反映的是形状保持、电平改变。
- **`f3_quiet_mix_first` 是反例而非支持证据**：它说明先混音后归一化不一定
  削波，取决于两源实际电平。
- 本分析不涉及视频路径，也不涉及并发/持久化语义。
- 按要求，**不要求修复**发现的处理性能/内存问题；本目录只做实测与归因。

---

## 9. 复现

所有步骤在 `analysis/wav-order-loss/` 下执行，使用纯标准库、无外部媒体程序：

```bash
# 1. 生成素材（含两份 25 MB 近上限输入）
python3 -I tools/materials.py --out-dir inputs --near-limit-bytes 25000000

# 2. 生成并校验实验矩阵（33 个全部通过服务的 compile_plan）
python3 -I tools/build_matrix.py --out matrix/trials.json

# 3. 运行实验：每个 trial 一个 RLIMIT_AS=192MiB 的独立子进程
python3 -I tools/run_experiment.py \
  --trials matrix/trials.json --inputs-dir inputs \
  --media-dir out/media --results out/runs/raw_results.json

# 4. 逐步中间态削波证据
python3 -I tools/step_trace.py --trials matrix/trials.json \
  --inputs-dir inputs --out out/runs/step_trace.json

# 5. 逐声道分析
python3 -I tools/analyze.py --results out/runs/raw_results.json \
  --trace out/runs/step_trace.json --out out/runs/analysis.json

# 6. 素材证据、可试听对照、配对清单、上传天花板
python3 -I tools/verify_materials.py --inputs-dir inputs \
  --out out/runs/materials_evidence.json
python3 -I tools/build_audition.py --results out/runs/raw_results.json \
  --analysis out/runs/analysis.json --out-dir out/audition
python3 -I tools/build_manifest.py --results out/runs/raw_results.json \
  --media-dir out/media --out out/runs/manifest.json
python3 -I tools/upload_ceiling.py --out out/runs/upload_ceiling.json
```

运行环境：Python 3.11.2，无 numpy/scipy/ffmpeg/sox。原项目
`avprovenance/` 与 `scripts/` 未做任何修改。
