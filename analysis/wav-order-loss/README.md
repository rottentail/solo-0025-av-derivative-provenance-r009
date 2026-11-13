# WAV 处理顺序实测分析

这是一次面向声音编辑的实测分析：用仓库现有服务（未改动）判断 WAV 配方中
增益、混音、归一化、裁剪的不同**合法顺序**，哪些会让安静细节或瞬态在成片
中不可逆丢失。

- **结论与完整报告**：见 [`REPORT.md`](REPORT.md)
- **可试听对照**：见 [`out/audition/`](out/audition/)
- **原始证据**：见 [`out/runs/`](out/runs/)

## 一句话结论

最终峰值相同**不代表**声音等价：同样到达 16384 峰值，干净顺序约 90 dB
SNR，错误顺序只有 12–16 dB；安静通道可以被压成数字静音，而做 `(L+R)/2`
折叠后仍看似 71.6 dB——所以本报告的所有指标都按声道分别给出，从不左右平均。

## 快速导读

| 想看什么 | 去哪 |
| --- | --- |
| 结论表与不可逆性判定 | `REPORT.md` §1 |
| 同峰值不同能量分布的素材验证 | `REPORT.md` §3, `out/runs/materials_evidence.json` |
| 各顺序族的逐声道 SNR / 差异 | `REPORT.md` §4, `out/runs/analysis.json` |
| 中途削波的逐步证据 | `out/runs/step_trace.json` |
| 192 MiB 预算边界与未完成组合 | `REPORT.md` §5, `out/runs/raw_results.json` |
| 可试听对照 | `out/audition/audition_index.json` |
| 媒体 ↔ 实际配方 的配对 | `out/runs/manifest.json` |

## 目录结构

```
analysis/wav-order-loss/
├── REPORT.md              # 报告与结论
├── README.md              # 本文件
├── tools/                 # 全部实验工具（纯标准库）
├── matrix/trials.json     # 33 个 trial 定义
├── inputs/                # 生成素材（含两份 25 MB 近上限输入）
└── out/
    ├── runs/              # 原始记录、分析、证据、清单
    ├── media/             # 服务真实输出 + 同名配方边车
    └── audition/          # 可试听对照
```

## 复现

见 `REPORT.md` §9。要点：每个 trial 在 `RLIMIT_AS=192 MiB` 的独立子进程里
驱动真实 HTTP 服务；未完成的组合保留阶段与内存归因，不从未完成中删除。

原项目 `avprovenance/` 与 `scripts/` **未做任何修改**。
