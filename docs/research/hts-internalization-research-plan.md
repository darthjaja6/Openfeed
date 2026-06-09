# 研究计划（定稿）：把物理约束内化进晶体扩散模型 —— 以常压常规超导体发现为验证

> 本文为定稿计划，取代《hts-generative-discovery-survey-and-plan.md》第 4 节的旧计划；文献依据见该调研报告第 1–3 节。
> 定稿日期：2026-06-09。

---

## 1. 定位（一段话）

当前最先进的超导体生成管线（Guided Diffusion, npj Comput. Mater. 2026）采样 200,000 个结构、经外部物理漏斗筛后仅存 773 个（合格率 ~0.4%）：物理约束完全位于模型之外。本研究把这些约束**内化进模型权重**——验证器（MLIP 稳定性、Tc 代理、对称性/电荷中性检查）只出现在**训练时**（RLVR / 偏好优化 / 蒸馏）与**最终评估时**（DFPT 金标准），采样时不再需要任何引导或筛选。**主指标是"裸采样合格率"**（unguided, unfiltered samples 的逐项物理合规率），目标是把发现漏斗缩小 1–2 个数量级；并以一组 DFPT 验证的常压稳定超导候选作为 discovery 演示，同时刻画"内化的边界"（哪些约束可达 100%、哪些有信息论天花板）与可能的涌现现象。

**核心论点（内化阶梯）**：

| 档 | 约束 | 内化上限 | 机制 |
|---|---|---|---|
| 按构造可达 100% | 空间群对称性、电荷中性 | 精确满足，对应筛选步骤**消失** | 硬约束架构（DiffCSP++/SymmCD 式）/ 约束采样 |
| 统计性、可大幅内化 | 热力学稳定（E_hull）、动力学稳定（声子） | 裸采样合格率 ~8% → 50–80% 量级 | RLVR / DPO / 搜索蒸馏，奖励 = MLIP |
| 有信息论天花板 | Tc | 只能内化到 Tc 代理所知的程度 | **权重装不进训练信号里没有的信息**；天花板的实证 = reward hacking 分析 |

## 2. 研究问题

- **RQ1（内化有效性）**：内化训练能把裸采样的逐项物理合规率提高多少？漏斗缩小几个数量级（等算力对照 CFG+后筛基线）？
- **RQ2（内化边界）**：内化阶梯三档的实证刻画——尤其 Tc 档的天花板：当 RL 把生成分布推离 Tc 代理训练域时，何时、如何发生 reward hacking（弱代理做奖励、强 held-out ensemble + OOD 检测做裁判）。
- **RQ3（涌现）**：内化后模型是否在未见化学空间自发满足约束？是否裸采样自发重现训练集中不存在的已知超导家族（时间外推/留族检验，仿 Konno 2021 重发现铁基超导的协议）？同时报告连续与阈值指标，对照 Schaeffer "海市蜃楼"批评。
- **RQ4（discovery 演示）**：裸采样直接产出的候选中，能否得到 N≥3 个 DFPT 验证（声子稳定 + 电声 Tc 过线）的常压、热力学（近）稳定新超导候选？

## 3. 范围与关键设计决策

1. **只做 BCS（电声）超导**——唯一有可计算、可验证 Tc 的体系；非常规超导不进奖励、不进主张。
2. **小原胞、高对称**（≤12 原子/胞）作为生成约束——使末端 DFPT 电声单个成本降到几十–几百 core·h（云 CPU $5–30/个），discovery 闭环在个人预算内闭合；这也符合 BCS 高 Tc 的物理偏好（MgB₂ 3 原子、Mg₂IrH₆ 9 原子）。
3. **化学空间**：常压、近凸包、轻元素（B/C/N）共价网络 + 金属化的三元/四元体系；**排除氢化物**（高压氢化物已被做透；常压稳定氢化物 Tc 上限 ~17 K；常压亚稳氢化物是 InvDesFlow 地盘且可合成性存疑）。Phase 0 用 Alexandria/Cerqueira 公开数据做 landscape 检查后最终锁定。
4. **基座与验证器全部复用开源**：生成器 DiffCSP / MatterGen（不从头训练）；MLIP 用 MatterSim / MACE-MP-0；Tc 代理自训于公开 DFPT 库（Cerqueira ~7000、JARVIS-SC 1058、BETE-NET 818），**必须带 ensemble 不确定性 + OOD 检测**（既是 RQ2 的测量仪，也是 DFPT 预算的弹药筛选器）。
5. **资源分工**：本地 M5 Mac Studio 64GB——全部 MLIP 验证、MLIP 声子、QE DFT 弛豫、分析出图（免费、不计时）；租用 GPU（RunPod/CUDA）——仅内化训练与批量采样；云 CPU spot——仅末端 DFPT burst。预算：GPU ~$300–600 + 云 CPU ~$200–500，**总计 < $1,000**。

## 4. 阶段计划（约 10 个月）

**Phase 0 — 基线测量与基础设施（第 1–6 周，本地为主）**
- 复现并测量**基线裸采样合规表**：预训练 DiffCSP / MatterGen 在无引导、无筛选下的逐项合规率（电荷中性 / 空间群 / E_hull<0.1 / MLIP 声子无虚频 / Tc 代理过线）——这张表是全文的 "X"。
- 搭统一 verifier oracle（SMACT → 间距 → MLIP 弛豫/E_hull → MLIP 声子 → Tc 代理），单一接口，训练与评估共用。
- 训练 Tc 代理（ensemble + OOD），在 held-out DFPT 库上报校准曲线。
- 化学空间 landscape 检查，锁定目标子空间。
- 交付：基线表 + oracle 服务 + 代理校准报告 + 数据卡。

**Phase 1 — 内化训练 ①②档（第 6–14 周，GPU 租用窗口 1）**
- ①档：对称性/电荷中性的硬约束化（换 DiffCSP++/SymmCD 式表示或约束采样），合规率 → 100%（按构造）。
- ②档：以 MLIP 稳定性（E_hull + 声子）为可验证奖励做 RLVR（GRPO/DDPO，奖励加权 KL + 经验回放 + 多样性过滤防坍缩）或 Diffusion-DPO（成对偏好 = 更低 E_hull）。
- 测量裸采样合规率提升与多样性/新颖性保持（防模式坍缩与"平凡衍生物"化，做 Szymanski & Bartel 式审计）。
- 交付：RQ1 主结果；可先以 workshop 短文/arXiv 占坑。

**Phase 2 — ③档天花板与涌现（第 14–22 周，GPU 租用窗口 2）**
- 把 Tc 代理加入奖励（不确定性门控：OOD 区域奖励衰减），扫 KL 强度 × 代理强弱 × OOD 距离，刻画 reward hacking 发生边界（RQ2）。
- 可选：测试时验证器搜索 → 蒸馏回权重（STaR/内化 CoT 式），检验搜索收益能否固化。
- 涌现协议（RQ3）：留族检验（训练时挖掉某已知超导家族，看裸采样是否重现）+ 未见化学空间合规率 + 连续/阈值双指标。
- 交付：RQ2 + RQ3 结果。

**Phase 3 — Discovery 演示（第 22–32 周，本地 + 云 CPU burst）**
- 内化模型裸采样 ~10⁴；本地 oracle 复核（注意：此处筛选只为分配 DFT 预算，不进主指标）；QE DFT 弛豫 ~50–100 个（本地）；声子确认 ~20–30；DFPT 电声 + Allen–Dynes/Eliashberg Tc 10–20 个（云 CPU）。
- 报告 N 个 DFPT 验证候选（RQ4），诚实标注 μ*、谐性近似、0K 凸包等 caveat，优先报告热力学稳定者。
- 交付：候选表 + 全管线开源。

**Phase 4 — 成文与投稿（第 32–40 周）**
- 主投 npj Computational Materials（discovery 演示 + 方法刻画的组合符合其口味）；备选 Digital Discovery / ML 会场 AI4Science。

## 5. 指标（防刷分）

- **主指标**：裸采样（无引导无筛选）逐项合规率 + 综合 S.U.N.-SC 率；漏斗缩小倍数（等算力 vs CFG+后筛基线）。
- 多样性 / 新颖性（对 MP/Alexandria 查重 + 占位无序检测 + ICSD 抽查审计）。
- Tc 代理 held-out MAE 与校准；DFPT 终验与代理的一致性。
- Reward hacking 证据链：弱代理分数 vs 强 ensemble 分歧 vs OOD 距离三联图。
- 涌现：留族重现率、未见空间合规率，连续+阈值双报告。

## 6. 风险与缓解（更新）

| 风险 | 缓解 |
|---|---|
| 抢发（最大风险；MatInvent/InvDesFlow/NYU-Flatiron 均活跃） | Phase 1 结果尽早 arXiv 占坑；主张差异化——竞品比漏斗吞吐，本工作比**裸采样合格率**，无人占位 |
| RL 刷 MLIP/代理而非真物理 | 不确定性门控 + KL 正则 + DFPT 抽检校准；hacking 本身是 RQ2 的数据 |
| 模式坍缩 / 平凡衍生物 | 多样性过滤 + 经验回放 + 新颖性审计入主指标 |
| DFPT 末端全军覆没 | 兜底主张退为 RQ1–RQ3（内化方法与边界刻画），投 Digital Discovery |
| MPS/算子兼容 | 训练只在 CUDA（租用）；本地只跑验证与 DFT |
| 涌现被指度量伪影 | 连续+阈值双指标，正面引用并回应 Schaeffer |

## 7. 论文标题与摘要（草稿）

**标题候选**（按推荐排序）：

1. *Internalizing physical constraints into crystal diffusion models: from post-hoc filtering to physics-aware generation of superconductors*
2. *From filtering to knowing: verifier-trained crystal diffusion shrinks the superconductor discovery funnel by two orders of magnitude*
3. *How much physics can a generative model internalize? Constraint internalization and its limits in diffusion-based superconductor discovery*

**Abstract 草稿**（npj Computational Materials 风格，~250 词；方括号为待实验回填的占位数字）：

> Generative models have begun to propose candidate superconductors, but current pipelines rely on massive post-hoc filtering: in state-of-the-art workflows, fewer than 1% of generated structures survive external physical screens for charge neutrality, symmetry, thermodynamic and dynamical stability, and electron–phonon-derived critical temperature. Here we ask whether these constraints can instead be internalized into the weights of a crystal diffusion model — in analogy to how reasoning behaviors are internalized into large language models through training on verifiable rewards — so that raw, unguided samples are physically valid by default. Combining symmetry- and charge-constrained architectures with reinforcement learning against verifiable physics rewards (machine-learned interatomic potentials for stability; an uncertainty-calibrated surrogate for the electron–phonon critical temperature), we raise the fraction of raw samples that pass the full physical screen from [X]% to [Y]%, shrinking the discovery funnel by [one-to-two] orders of magnitude at matched compute. We find a three-tier "internalization ladder": exactly computable constraints (space-group symmetry, charge neutrality) can be internalized completely; statistical constraints (stability) can be internalized substantially; whereas the critical temperature is bounded by the knowledge of its surrogate — pushed beyond its validity domain, optimization reliably produces reward hacking rather than physics, which we characterize with held-out ensembles and out-of-distribution diagnostics. The internalized model, sampled without any guidance or filtering, directly yields [N] ambient-pressure, (near-)hull-stable candidate conventional superconductors validated by density-functional perturbation theory with predicted Tc up to [Z] K. We release the full pipeline, establishing raw-sample validity — rather than funnel throughput — as a benchmark for physics-aware generative materials discovery.

**一句话 pitch**（cover letter 用）：
> 把"生成后物理筛选"从推理时挪进训练时，证明哪些物理约束能被生成模型真正学会、哪些存在信息论天花板，并以 DFPT 验证的常压超导候选演示其发现能力。

---

## 8. 立即行动清单（本周）

1. 本地环境：`uv`/`conda` 装 pymatgen + SMACT + MACE-MP-0 + MatterSim + phonopy + ASE；编译 QE（ARM/macOS）。
2. 下载：DiffCSP、MatterGen 权重；Alexandria 子集、MP；Cerqueira DFPT 库、JARVIS-SC、BETE-NET 818。
3. 跑出第一版**基线裸采样合规表**（预训练模型 1000 样本，本地全栈验证）——全项目的第一个数字。
4. landscape 检查锁定化学子空间。
