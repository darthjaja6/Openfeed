# Diffusion vs. Autoregression in Language Models: One Kernel, Different Bases

> 研究计划 / Research Plan
> 主题:在语言模型中检验「diffusion 与 autoregression 是同一套内核在不同基函数空间中的展开」,
> 并据此探索一个加速推论(寻找适合 ODE flow marching 的"好基")。

本目录包含本研究的完整计划。文档拆成几个文件,便于分别迭代:

| 文件 | 内容 |
|---|---|
| `README.md`(本文件) | 概览、目录、一句话总览 |
| `01-goal.md` | 研究 goal:北极星、可操作化 hypothesis、研究问题、scope、判据 |
| `02-background.md` | 背景与相关工作(已知等价性、谱自回归、表征对比、加速线) |
| `03-methodology.md` | 方法论:换基算子、对齐度量、crosscoder、可学基 + 直度正则 |
| `04-experiments.md` | 实验设计:模型对、数据、消融、里程碑 |
| `05-references.md` | 参考文献 |

## 一句话总览

已知(分布层面):masked/absorbing diffusion ≡ any-order autoregression。
**本研究问的是机制层面**:训练好的 DLM 与 AR LLM 是否学到了**可经一个显式换基算子相互对齐的同一内部内核**;
换基算子是**线性/正交**还是必须**高度非线性**——这个分叉本身就是核心可交付的发现。
作为推论,探索是否存在一组让 probability-flow ODE 路径接近直线的"好基",从而显著提升训练/采样效率。

## 状态

- [x] 立项 / goal 定稿(`GOAL.md` 电梯版,`01-goal.md` 详细版)
- [x] 第一步 harness:对齐协议 + 指标 + RQ1/cheap-RQ2(toy 已跑通,见 `code/`)
- [x] 自动判定模块(`code/analyze.py`):结果 JSON → STRONG_LINEAR / PARTIAL_NONLINEAR / FALSIFIED
- [x] RQ3 机制 toy 验证(`code/rq3_toy.py`):coupling +35.9%、可学基再 +6.3% 让 ODE 场变直
- [x] 真实模型驱动(`code/pod_experiment.py`)+ RunPod 编排(`code/runpod_run.py`,dry-run)就绪
- [ ] **在 RunPod 上实跑** same-source Dream 对 → 得到第一个真实判定(需算力开销 + HF token)
- [ ] RQ2 完整版:crosscoder / SAE 分解共享内核 vs 模型特有
- [ ] RQ3 完整版:真实 latent 上的可学基 + 直度联合优化

代码见 `code/`(`python test_pipeline.py` 本地即可验证管线)。
