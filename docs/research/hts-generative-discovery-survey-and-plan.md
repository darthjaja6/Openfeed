# 用 Diffusion / Transformer 发现高温超导体：文献调研与研究计划

> 主题：以生成式模型（diffusion / flow / 自回归 transformer / GFlowNet）发现新化合物，尤其是超导体；并探索把"生成后物理约束筛选"这一外部脚手架**内化进模型训练**（类比 LLM 的 CoT 内化、RLVR、推理涌现）的可行路径。
>
> 完成日期：2026-06-09。本文由五路并行文献调研综合而成。

---

## 0. 一句话结论（写在最前）

当前超导体生成的最前沿（`Guided Diffusion for Superconductors`, npj 2026 / arXiv:2509.25186）仍停留在
**"classifier-free guidance 条件生成 → 大规模 ML/DFT 多级后筛"** 的范式：物理约束完全是
**生成之后**的外部漏斗，没有进入模型权重。而通用 ML 领域（LLM 的 RLVR/CoT 内化、diffusion 的 RL 微调与测试时验证器搜索、
蛋白设计的 Feynman–Kac steering + 测试时扩展四件套）已经发展出成熟的"把验证器内化进策略 / 在采样时主动搜索"的方法。
**把这两条线接起来——用可验证的物理奖励（稳定性 / 电声 Tc）对晶体扩散做 RLVR 式内化，或做测试时噪声搜索 + 物理验证器——
是一个干净、可行、尚未被占领的空白。** 本文给出三个递进的研究方向与一个 18 个月的执行计划。

⚠️ **核查声明**：调研环境下 `WebFetch` 对 arxiv.org / nature.com / openreview 等学术域名整体返回 403，无法逐字抓取全文。
下文所有条目均经多源检索摘要（arXiv 列表页、Nature、OpenReview、NeurIPS proceedings、GitHub README）交叉核对标题/作者/年份/编号；
凡仅靠摘要片段确认、未经全文逐字核验者，已在原始调研中标注。引用关键数字前建议到对应 URL 复核。

---

## 1. 背景：超导体发现这件事到底难在哪

理解机会之前，先要理解三个硬约束，它们决定了所有方法的天花板：

1. **机制天花板（最关键）**：唯一可以"算"出来的 Tc，是基于电声耦合的 Migdal–Eliashberg / Allen–Dynes 框架，
   **只对常规 BCS 超导成立**。铜氧化物、铁基、镍氧化物等非常规超导**没有可计算的 Tc 泛函**，
   纯数据驱动模型对它们只能做"家族内插值"，没有外推依据。→ 任何"内化物理约束"的方案，
   其"可验证奖励"目前实际上只能建立在 **BCS 超导**上。

2. **数据双轨制**：
   - 实验数据（`SuperCon` / `SuperCon2` / `3DSC` / `SuperConD`）噪声大、常缺结构、几乎不记录压力字段；`SuperCon` 约有上万条重复记录、约 7000 条缺 Tc。
   - 计算数据（`JARVIS-SC` 1058 条、Cerqueira ~7000 条 DFPT 库、`BETE-NET` 818 条 α²F）干净但**小**且仅覆盖 BCS。
   - 两轨标签语义不同（实验 Tc vs Allen–Dynes Tc），混训需谨慎。

3. **稳定 ≠ 可合成 ≠ 有用**：0 K 有序 DFT 凸包稳定的"新材料"，大量是已知无序固溶体被 DFT 错误"排序"成的虚构有序相
   （`Cheetham & Seshadri` 2024、`Leeman/Palgrave/Schoop` PRX Energy 2024、`Szymanski & Bartel` 2026 三篇批评文一致指向此盲点；
   连 MatterGen 的招牌合成物 TaCr₂O₆ 都被指出等价于 1972 年已报道的无序相、且就在训练集中）。
   高 Tc 常压氢化物几乎都以**亚稳**为代价（GNoME 库中真正热力学稳定的常压氢化物最高 Tc 仅 ~17 K）。

> **设计启示**：本课题真正有价值的"内化"目标，不应只是"形成能 < 阈值"这种容易被刷的指标，
> 而应是更难、更接近物理真值的复合奖励：**热力学稳定 + 动力学稳定（无虚频声子）+ 电声 Tc + 可合成性 + 新颖性**。
> 这正好对应 LLM 里"可验证奖励（verifiable reward）"的角色。

---

## 2. 文献地图（四大板块）

### 2.1 晶体结构生成模型主线（生成器候选）

按"物理先验如何内化"演进排列：

| 模型 | 年份/出处 | 类别 | 晶体表示 | 内化的物理先验 | 条件/引导机制 | 链接 |
|---|---|---|---|---|---|---|
| **CDVAE** | ICLR 2022 | VAE + score 解码 | 晶格+坐标+种类 | SE(3) 等变、周期性、"稳定=局部能量极小" | 隐空间属性梯度优化 | arXiv:2110.06197 |
| **DiffCSP** | NeurIPS 2023 | 联合等变扩散 | **分数坐标**+晶格+种类 | periodic-E(3) 等变、Wrapped Normal 处理周期平移 | 主要做 CSP | arXiv:2309.04475 |
| **DiffCSP++** | ICLR 2024 | 空间群约束扩散 | 晶格对数空间基约束 + Wyckoff 坐标约束 | **230 空间群硬约束** | 定制空间群可控生成 | arXiv:2402.03992 |
| **MatterGen** | Nature 2025 | 定制扩散 | 离散种类+wrapped 坐标+对称化晶格扩散 | 等变 score、周期性、晶格对称化 | **adapter 微调 + classifier-free guidance**（化学/对称/力学/电子/磁学） | arXiv:2312.03687 |
| **UniMat** | ICLR 2024 | 扩散（U-Net 式） | 周期表张量统一表示 | 用周期表行列做化学先验（非等变） | 组成条件；**首个百万级**扩散 | arXiv:2311.09235 |
| **SymmCD** | ICLR 2025 | 连续+离散混合扩散 | 非对称单元 + 位点对称 | 晶体学对称性显式入模、跨空间群共享 | — | arXiv:2502.03638 |
| **WyckoffDiff** | 2025 | 纯离散扩散（D3PM） | 只生成 Wyckoff protostructure | 对称性**按构造**满足 | — | arXiv:2502.06485 |
| **SCIGEN** | Nature Mater. 2025 | 采样期约束注入（基于 DiffCSP，免再训练） | masking/inpainting 引导 | 强制 honeycomb/kagome/Lieb 等几何基序 | 生成 1000 万→DFT→**实验合成** TiPd₀.₂₂Bi₀.₈₈ 等 | arXiv:2407.04557 |
| **FlowMM / FlowLLM** | ICML/NeurIPS 2024 | 黎曼流匹配 / LLM+RFM | 环面流形坐标 | 对称性推广到流匹配 | FlowLLM 稳定率~3×SOTA、S.U.N.+50% | arXiv:2406.04713 / 2410.23405 |
| **CrysBFN** | ICLR 2025 Spotlight | 周期贝叶斯流网络 | 超环面周期 BFN | 周期 E(3) 等变 | MP-20 上 **~200× 采样加速** | arXiv:2502.02016 |
| **OMatG** | ICML 2025 | 随机插值（统一扩散/流） | 等变图 + 离散流匹配种类 | 扩散与流匹配为其特例 | CSP + DNG 双 SOTA | arXiv:2502.02582 |
| **CrystalFlow** | Nature Comm. 2025 | 条件流匹配 | 晶格+坐标+种类 | 等变 GNN | **压强条件**（高压 CSP，CALYPSO 背景） | arXiv:2412.11693 |
| **Crystal-GFN** | 2023（Bengio 组） | GFlowNet | 顺序采样空间群→组成→晶格（无坐标） | 状态空间可施加**电荷中性/空间群**硬约束 | 以形成能 proxy 为奖励，按奖励配比采样 | arXiv:2310.04925 |
| **CrystaLLM** | Nature Comm. 2024 | 自回归 GPT | **直接对 CIF 文本建模** | 对称性以空间群 token 由数据学习 | DFT 验证；MCTS 解码 | arXiv:2307.04340 |
| **crystal-text-llm** | ICLR 2024 | LoRA 微调 LLaMA-2(70B) | 晶格+元素+坐标字符串 | 对称性随规模涌现 | ~90% 满足电荷/位置约束；亚稳率49% | arXiv:2402.04379 |
| **CrystalFormer** | Science Bulletin 2025 | 自回归 transformer | 空间群条件下顺序生成 Wyckoff 原子 | Wyckoff 序列压缩晶体空间 | 即插即用属性引导；有 RL 微调后续 | arXiv:2403.15734 |
| **ADiT** | ICML 2025 | 隐空间 Diffusion Transformer | VAE 把分子+晶体编码到共享隐空间 | **刻意最小化归纳偏置**，靠规模 | classifier-free guidance；0.5B 可扩展 | arXiv:2503.03965 |
| **Chemeleon** | Nature Comm. 2025 | 文本引导扩散 | Crystal CLIP 文本-晶体对齐 | — | 文本 + CFG（"trigonal Zn–Ti–O"） | arXiv:2509.02723(综述内) |

**趋势线**：等变扩散 → 对称性显式硬约束（Wyckoff/空间群） → 流匹配/BFN 提效 → LLM 文本/混合管线 → 弱归纳偏置+规模化统一模型 → **推理期引导/搜索/RL 的免微调 steering**。

**验证器侧基础设施**：`MatterSim`（MLIP，0–5000K/0–1000GPa）、`OMat24`+EquiformerV2（1.1 亿 DFT 数据）、`M3GNet`/`CHGNet`/`MACE-MP-0`/`Orb`；指标 `SMACT`（电荷中性）、`S.U.N.`（Stable/Unique/Novel）；基准 `MatBench Discovery`、`LeMat-GenBench`。

### 2.2 超导体专门工作（任务与现状）

**Tc / 电声耦合预测器（"验证器"候选）**：
- `Stanev et al.` 2018（npj）：Magpie 成分特征随机森林，分族建模，纯成分级，跨家族外推弱。
- `BETE-NET` 2024（npj，arXiv:2401.16611）：等变 GNN 自举集成直接预测 α²F(ω)→λ,ω_log→Allen–Dynes Tc；仅 818 个 DFPT 训练；MAE: λ 0.21、Tc 2.5 K。
- `BEE-NET` 2026（npj，arXiv:2503.20005）：BETE-NET 升级，Eliashberg 谱预测 Tc MAE **0.87 K**、真阴性率 99.4%，嵌入 130 万候选→MLIP→Tc→DFPT 流水线，得 **741 个 DFT 确认 Tc>5K**，并合成验证两种材料。
- `Choudhary & Garrity` 2022（JARVIS-SC，1058 DFPT 库 + ALIGNN）、`Cerqueira/Sanna/Marques` 2024（~7000 DFPT 库，扫 20 万化合物得 541 个 Tc>10K，亮点 LiMoN₂）。
- `BNAS` 2025：从费米面能带 3D 体数据回归 log Tc，R²=0.887，23 个新合成超导体验证。

**生成式 × 超导（与本课题最直接相关）**：
- `Inverse Design ... Deep Generative Models`（Wines/Xie/Choudhary, 2023, arXiv:2304.08446）：**首批**把 CDVAE 对准超导（~1000 JARVIS-SC 训练→3000 生成→ALIGNN 筛→61 候选→DFT）。
- `SuperDiff`（2024, Sci. Rep., arXiv:2402.00198）：DDPM+ILVR **成分级**生成"假想新超导家族"，无结构→无法做声子/电声验证。
- `DiffCSP-SC` / `SODNet`（NeurIPS 2024, openreview iNYrB3ip9F）：SE(3) 等变 + 自建 `SuperCon3D`（含无序占位结构）。
- `InvDesFlow` / `InvDesFlow-AL`（胡江平组, 2024–2025, npj 2025 arXiv:2505.09203）：扩散生成+分类器+形成能+Tc回归+DFT，加主动学习闭环；具名候选 **Li₂AuH₆（常压 ~140K）**、K₂GaCuH₆/Na₂LiAgH₆ 等（均亚稳、无实验）。
- **`Guided Diffusion for Superconductors`（NYU/UF/Flatiron, npj 2026, arXiv:2509.25186）= 当前 SOTA**：DiffCSP 在 Alexandria（>200 万）预训练，在 **7183 个第一性 Tc 标签**超导体上微调，**classifier-free guidance** 按 Tc 条件采样 20 万→3.4 万唯一→ML+DFT 多级→**773 个 DFT Tc>5K**。本质仍是"生成后筛选"。
- **MatterGen × 超导：尚无同行评审论文**（仅社区项目用 3DSC 微调，未验证）→ 也是一个空位。

**实验闭环**：`Pogue et al.` 2023（npj，闭环 4 轮，在 Zr–In–Ni 体系发现一个实验证实的新超导体）。明星候选 Mg₂IrH₆（160K）至今**未被合成**；已合成的 Mg₂IrH₅ 是绝缘体。

### 2.3 约束/奖励"内化"方法（本课题方法论核心）

**A) Diffusion 的奖励/偏好微调**：
- `DDPO`（Black 2023, arXiv:2305.13301）：把去噪建成多步 MDP，策略梯度优化任意（含不可微）奖励。
- `DPOK`（2023）、`DRaFT`（arXiv:2309.17400，可微奖励整链反传，DRaFT-K 截断）、`AlignProp`（arXiv:2310.03739）、`Diffusion-DPO`（CVPR, arXiv:2311.12908，成对偏好直接优化）。
- `GFlowNet`（Bengio 2021）：按奖励配比采样、强调多样高奖励解，分子设计旗舰。
- **材料端落地迹象**：`MatInvent`/"Accelerating inverse design with RL"（2025, arXiv:2511.03112，MatterGen 为底座 + RL，奖励加权 KL，最高 378× 减少性质计算，但做的是带隙/磁/力学**通用**性质，**未做超导**）；`CrystalGym`（arXiv:2509.23156，含 DFT 反馈的 RL 基准）；`OMatG-IRL`（arXiv:2602.00424，对流模型速度场直接推理期策略梯度 RL，首次 RL 用于 CSP）。

**B) 引导（Guidance）**：
- `Classifier guidance`（Dhariwal 2021）、`Classifier-Free Guidance`（Ho 2022, arXiv:2207.12598）。
- `SVDD`（NeurIPS 2024, arXiv:2408.08252）：**推理期免微调、免可微代理**，soft value 前瞻 + best-of-N，可用非可微反馈（docking/QED），已用于分子/DNA/RNA——**晶体上未见**。
- 晶体能量/力场引导扩散（如 arXiv:2503.10471 Siamese FM）；自适应约束引导（arXiv:2604.13354，在预训练 MatterGen 上采样期耦合可微约束损失，免微调）。

**C) 硬约束架构（把物理"内化"进结构）**：DiffCSP++、SymmCD、Space Group Equivariant Diffusion（arXiv:2505.10994）已内化**对称性**；E(n)-EDM（Hoogeboom 2022）等变内化平移/旋转/置换。
**但电荷中性流形、化学计量、能量壳层的 mirror/manifold diffusion 硬约束尚未与超导生成结合。**

**D) LLM 侧类比（"内化"的思想源头）**：
- `Internalize CoT Step by Step`（Deng 2024, arXiv:2405.14838）：训练中**逐步删去中间推理 token**，把显式推理压入隐藏状态——"把外部脚手架内化进权重"的核心范式。
- `STaR`（Zelikman 2022）：生成推理链→答案正确性过滤→正确链回填微调，迭代自举。
- `DeepSeek-R1`（Nature 2025, arXiv:2501.12948）：**RLVR**，仅用可验证奖励 + GRPO，推理能力涌现。
- `Coconut`（arXiv:2412.06769）：连续隐空间推理。
- 涌现之争：`Emergent Abilities`（Wei 2022）vs `Are Emergent Abilities a Mirage?`（Schaeffer 2023，指出多为非线性度量伪影）。

**E) Diffusion 推理 / 测试时扩展**：
- `Diffusion-of-Thought`（NeurIPS 2024, arXiv:2402.07754）、`LLaDA`（arXiv:2502.09992）、`d1`（arXiv:2504.12216，diffu-GRPO 把 RL 推理内化进扩散 LLM）。
- **`Inference-Time Scaling for Diffusion beyond Denoising Steps`（Ma et al., 2025, arXiv:2501.09732）**：固定去噪步数，转而**在初始噪声上搜索**；设计空间 = 验证器 × 搜索算法（best-of-N / 零阶 / 路径）。→ 可直接迁移到晶体（验证器 = MLIP/DFT/Tc）。
- `Diffusion Inference Scaling through Classical Search`（arXiv:2505.23614，树搜索 + Langevin 局部精炼）。
- `Feynman–Kac steering`（蛋白, arXiv:2511.09216）、`Proteina-Complexa`（NVIDIA, ICLR 2026）：**测试时扩展四件套**（best-of-N / beam / FK steering / MCTS）+ 结构置信度奖励——科学生成最完整范例，**晶体上无等价系统工作**。
- `AMix-1`（蛋白, arXiv:2507.08920）：scaling law + **涌现能力分析** + 进化式测试时扩展——材料基础模型可借鉴的方法论模板。
- `Self-Refine`（NeurIPS 2023）：生成→自我批评→修订，无需训练。

### 2.4 生成后筛选 pipeline 与成本结构（机会的成本依据）

事实标准漏斗与成本（数量级速查）：

| 阶段 | 单结构成本 | 典型淘汰/存活 |
|---|---|---|
| SMACT 电荷中性 / 最小间距(>0.5Å) | 毫秒–秒 | 形式过滤，主流模型存活 ≈83–100%（批评：0.5Å 太松，几乎不淘汰） |
| 去重/查新（MP/Alexandria 580万/OQMD） | 秒 | MatterGen：唯一 86–100%、新颖 ~68% |
| **MLIP 预弛豫**（M3GNet/CHGNet/MACE/MatterSim/Orb/eqV2） | 0.03–0.24 GPU·h（**比 DFT 快 10³–10⁴**） | 粗筛 E_hull，送 DFT 前压缩 1–2 个数量级 |
| DFT 弛豫 + 凸包（E_hull<0.1 eV/atom 亚稳；≤0 稳定） | ~10–10² core·h | MatterGen：78% <0.1、13% 在凸包上；生成模型经 CHGNet 预筛后真稳定仅 ~8–22% |
| 声子（动力学稳定，无虚频） | 弛豫的 10–100 倍 | 大流水线**普遍跳过**；数据集虚频率 2–6%；MLIP 已可代算 |
| **DFPT + EPW（超导电声）** | DFPT ~180 + EPW ~3500 core·h；细网格 10⁴–10⁶ core·h | 只对 O(10²–10³) 末端候选可行 |

**关键事实**：瓶颈已从"生成"彻底转移到"验证"。MLIP 把预筛降低 3–4 个数量级，使 GNoME 式 10⁹ 候选漏斗成为可能。
**但最便宜的环节（0K 有序 DFT 凸包）反而最不可靠**（无序固溶体误判、新颖性失真、可合成性缺位）。

横向稳定率基线（`Szymanski & Bartel` 2025, arXiv:2501.02144）：CHGNet 预筛后送 DFT 的稳定率——FTCP 22%、CrystaLLM 17%、扩散类 ~8%；**传统离子交换基线在"稳定且新颖"上反而优于全部生成模型**（但产物多与已知物相似）。

可合成性预测：`Jang` 2020（PU+GCN）、LLM 法（Nat. Comm. 2025，准确率 98.6%、前驱体推荐 >90%）。

---

## 3. 空白点分析（机会在哪）

综合五路调研，以下组合在公开文献中**未见或极罕见**（按可行性 × 新颖性排序）：

| # | 空白点 | 信心 | 类比的成熟方法 |
|---|---|---|---|
| **G1** | **RLVR 式"可验证物理奖励 + 晶体扩散"用于超导**：以 BETE-NET/BEE-NET 类 Tc 预测器 + MLIP 稳定性作 verifier，GRPO/DDPO 微调 DiffCSP/MatterGen。超导端目前只有 CFG 后筛（2509.25186），MatterGen+RL（MatInvent）只做通用性质。 | **高** | DeepSeek-R1 RLVR；d1 的 diffu-GRPO；DDPO |
| **G2** | **测试时噪声搜索 + 物理验证器**（Ma et al. 2501.09732 框架）用于晶体/超导：固定去噪步数，在初始噪声上做 best-of-N / 零阶 / 路径搜索，验证器 = MLIP 能量 + 凸包 + Tc。图像/蛋白成熟，晶体未见。 | **高** | Ma et al. 2025；Proteina-Complexa 四件套；SVDD |
| **G3** | **引导/验证器蒸馏回权重**（"内化 CoT"的迁移）：先用昂贵的外部能量/力场/Tc 引导或测试时搜索生成高质量样本，再把这些样本/这套搜索行为**蒸馏回扩散权重**，使采样时无需外部验证器。LLM 有 STaR/内化 CoT，扩散材料侧几乎空白。 | 中高 | Deng 2024 内化 CoT；STaR；DRAKES/迭代蒸馏(2507.00445) |
| **G4** | **Diffusion-DPO / 偏好优化用于晶体超导**：用"更低 E_hull / 更高 Tc"构造成对偏好做 DPO 微调。蛋白/分子已落地，晶体超导未见。 | 中高 | Diffusion-DPO；PLaID++(2509.07150) |
| **G5** | **Feynman–Kac steering / twisted SMC 粒子滤波**用于晶体扩散：以 -能量 / Tc 作势倾斜采样轨迹。蛋白成熟，晶体未见。 | 中 | FK steering(2511.09216)；Proteina-Complexa |
| **G6** | **mirror/manifold diffusion 把硬物理约束内化**（电荷中性流形、能量壳）用于超导。DiffCSP++/SymmCD 只内化了对称性。 | 中 | Mirror Diffusion(2310.01236) |
| **G7** | **Diffusion-of-Thought 式"显式中间推理链"用于晶体**：先推理空间群/化学计量/配位环境，再生成结构；以及 Self-Refine 式自批评-修订训练进生成器。 | 中（探索性） | DoT；Self-Refine |
| **G8** | **测试时扩展的 scaling 曲线 / 涌现分析**（随验证预算增长的性能曲线，AMix-1 已对蛋白做）在晶体/超导生成中的系统刻画。 | 中（科学问题） | AMix-1；Wei vs Schaeffer 涌现之争 |

**判断**：G1 和 G2 是最干净、可行性最高的切入点；G3 是最贴合用户"把外部约束内化、追求涌现"原始动机、也最具论文新颖性的方向。建议把 **G2（先做，作为基础设施与 baseline）→ G1（核心贡献）→ G3（升华与涌现叙事）** 串成一条主线。

---

## 4. 研究计划

### 4.1 研究问题（提炼自用户动机）

> **物理约束能否、以及如何被"内化"进生成模型的权重，使模型在采样时无需外部 DFT/筛选漏斗就能直接产出物理上成立的超导体候选？这个过程是否会出现 reasoning model 式的涌现（如自发学会对称性、电荷中性、稳定性判据）？**

拆成三个可证伪的子问题：
- **RQ1（内化是否有效）**：用可验证物理奖励对晶体扩散做 RLVR/DPO 微调，能否在"采样即合规"的指标（不经外部筛选的 S.U.N.、Tc 命中率）上显著超过 CFG + 后筛？
- **RQ2（测试时 vs 训练时）**：测试时验证器搜索（G2）与训练时内化（G1）在"算力-质量"曲线上如何 trade-off？把测试时搜索蒸馏回权重（G3）能否把搜索收益"固化"？
- **RQ3（涌现）**：随模型/数据/奖励预算增大，是否出现物理合规能力的非线性跃升？（并用连续指标对照 Schaeffer 的"海市蜃楼"批评，避免度量伪影。）

### 4.2 范围界定（务实）

- **目标体系限定为常规（BCS / 电声）超导**——因为这是唯一有可计算、可验证 Tc 的体系，是 RLVR 成立的前提。
  非常规超导（铜氧/镍氧）放到"讨论/未来工作"，只做数据驱动定性探索，不作为可验证奖励。
- **生成器基座**：优先 **DiffCSP / MatterGen**（开源、等变、已有超导微调先例），LLM 路线（CrystalFormer/CrystaLLM）作为对照。
- **验证器栈**：MLIP（MatterSim / MACE-MP-0 / Orb）做稳定性与声子粗筛；BETE-NET/BEE-NET 类做 Tc 代理；末端用 DFT+DFPT（EPW）做金标准抽样核验。

### 4.3 阶段计划（约 18 个月）

**Phase 0 — 基础设施与复现（1–2 月）**
- 复现 `Guided Diffusion for Superconductors`（2509.25186）作为 baseline；跑通 DiffCSP/MatterGen + MatterSim 评估管线。
- 搭建统一验证器服务：SMACT → 最小间距 → MLIP 弛豫/E_hull → MLIP 声子（虚频检测）→ Tc 代理（自训或复用 BETE-NET）。把它做成**可微/可查询的 reward oracle**，供后续 RL/搜索/引导共用。
- 整理数据：Alexandria 预训练集 + 7183 超导 Tc 标签 + JARVIS-SC + Cerqueira DFPT 库；明确实验/计算双轨标签的使用边界。
- **交付物**：可复现 baseline + reward oracle 服务 + 数据卡（含噪声说明）。

**Phase 1 — G2 测试时验证器搜索（2–4 月）**
- 实现 Ma et al. 框架于晶体扩散：在初始噪声上做 best-of-N / 零阶 / 路径搜索，验证器 = 复合物理奖励（稳定性 + Tc + 新颖性）。对比 SVDD（soft value 前瞻）、FK steering（G5）。
- 画"验证预算 vs 不经后筛的命中率"曲线（回答 RQ2 前半、RQ3 雏形）。
- **交付物**：测试时搜索模块 + 一篇 workshop 短文级结果（"test-time scaling for crystal/superconductor generation"，本身即填补空白）。

**Phase 2 — G1 RLVR 内化（核心，4–8 月）**
- 用 Phase 0 的 reward oracle 作 verifiable reward，对 DiffCSP/MatterGen 做 GRPO/DDPO 微调（参考 d1 的 diffu-GRPO、MatInvent 的奖励加权 KL + 经验回放 + 多样性过滤防坍缩）。
- 复合奖励课程（curriculum）：先稳定性 → 加声子动力学稳定 → 加 Tc → 加新颖性/可合成性，观察能力是否分阶段出现（涌现信号）。
- 关键对照：CFG+后筛（baseline）、测试时搜索（Phase 1）、RLVR 内化（本阶段）在**等算力**下的命中率与多样性。
- 诊断"内化"：探针实验——微调后模型在**无引导**采样下的电荷中性率、空间群合规率、E_hull 分布是否系统改善（证明约束进了权重）。
- **交付物**：主结果论文初稿（RQ1 + RQ2）。

**Phase 3 — G3 验证器/搜索蒸馏 + 涌现研究（8–14 月）**
- 把 Phase 1 测试时搜索产生的高分样本（或搜索策略）**蒸馏回**生成器权重（自蒸馏/expert-iteration/STaR 式回填），检验能否在采样时去掉外部验证器仍保留收益（RQ2 后半，最贴合"内化 CoT"叙事）。
- 探索 G7：让生成器先输出"中间推理"（空间群/化学计量/配位）再出结构（Diffusion-of-Thought 式），看是否提升合规与可解释性。
- **涌现实验（RQ3）**：沿模型规模/数据量/奖励预算三轴扫描，报告物理合规能力曲线；**同时报告连续指标（能量分布、凸包距离）与阈值指标（命中率）**，正面回应 Schaeffer 的度量伪影批评。
- **交付物**：完整论文（含涌现分析）+ 开源代码与基准。

**Phase 4 — DFT/DFPT 金标准核验与（可选）实验对接（14–18 月）**
- 对内化模型无引导直接产出的 Top 候选做 DFT 弛豫 + 声子 + DFPT 电声（EPW）金标准核验，报告与 reward oracle 的偏差。
- 诚实处理可合成性与"无序相误判"陷阱（强制 ICSD/MP 查重、报告占位无序风险），避免重蹈 GNoME/TaCr₂O₆ 覆辙。
- （若有实验合作）选 1–2 个常压、热力学稳定（非亚稳氢化物）候选尝试合成。

### 4.4 评估指标（防刷分）

- **不经外部后筛**的：S.U.N. 率、电荷中性率、空间群合规率、E_hull 分布、声子虚频率、Tc>阈值命中率、多样性（防模式坍缩）、新颖性（对 MP/Alexandria 查重）。
- **算力归一化**：所有方法在等 GPU/CPU·h 预算下比较（验证-生成算力分账）。
- **金标准抽样核验**：DFT/DFPT 对模型自评 Tc 的 MAE。
- **批评对照**：强制做 `Szymanski & Bartel` 式审计（随机抽 N 个"新"候选查 ICSD），报告"真新颖"比例；用连续指标对照阈值指标。

### 4.5 主要风险与缓解

| 风险 | 缓解 |
|---|---|
| **机制天花板**：可验证 Tc 仅限 BCS | 明确限定范围；非常规超导只做定性数据驱动，不进奖励 |
| **奖励黑客（reward hacking）**：模型刷 reward oracle 而非真物理 | KL 正则 + 多样性约束 + 周期性 DFT 金标准核验校准 oracle |
| **Tc 代理误差大**（Allen–Dynes 强耦合区偏差、μ* 任意） | 奖励用"稳定性优先、Tc 为软目标"；末端 DFPT 核验 |
| **无序相误判**（GNoME/TaCr₂O₆ 教训） | 强制查重 + 占位无序检测 + 可合成性筛 |
| **模式坍缩**（RL 微调常见） | 经验回放 + 多样性过滤（仿 MatInvent） |
| **数据小**（超导计算标签仅千级） | Alexandria 大规模预训练 + 迁移；主动学习扩库 |
| **涌现是度量伪影**（Schaeffer 批评） | 连续 + 阈值指标双报告 |

### 4.6 预期贡献

1. **首个**把 RLVR/可验证物理奖励**内化**进晶体扩散用于**超导**的系统工作（G1）。
2. **首个**晶体/超导生成的**测试时验证器搜索**框架与"算力-质量"曲线（G2）。
3. 验证器**蒸馏回权重**的"约束内化"范式（G3），并给出材料生成中**涌现**的实证刻画（RQ3）。
4. 一套防刷分、含批评性审计的评估协议与开源基准。

---

## 5. 关键文献清单（按主题，附链接）

**晶体生成主线**：CDVAE arXiv:2110.06197 · DiffCSP arXiv:2309.04475 · DiffCSP++ arXiv:2402.03992 · MatterGen Nature 2025 / arXiv:2312.03687 · UniMat arXiv:2311.09235 · SymmCD arXiv:2502.03638 · WyckoffDiff arXiv:2502.06485 · SCIGEN Nature Mater. / arXiv:2407.04557 · FlowMM arXiv:2406.04713 · FlowLLM arXiv:2410.23405 · CrysBFN arXiv:2502.02016 · OMatG arXiv:2502.02582 · CrystalFlow arXiv:2412.11693 · Crystal-GFN arXiv:2310.04925 · CrystaLLM arXiv:2307.04340 · crystal-text-llm arXiv:2402.04379 · CrystalFormer arXiv:2403.15734 · ADiT arXiv:2503.03965 · GNoME Nature 2023 · MatterSim arXiv:2405.04967 · OMat24 arXiv:2410.12771

**超导专门**：Stanev 2018 npj · BETE-NET arXiv:2401.16611 · BEE-NET arXiv:2503.20005 · Choudhary&Garrity 2022 npj · Cerqueira 2024 Adv.Mater. · BNAS arXiv:2409.07721 · CDVAE-超导 arXiv:2304.08446 · SuperDiff arXiv:2402.00198 · SODNet/DiffCSP-SC openreview:iNYrB3ip9F · InvDesFlow arXiv:2409.08065 · InvDesFlow-AL arXiv:2505.09203 · **Guided Diffusion Superconductors arXiv:2509.25186 / npj 2026** · Pogue 闭环 2023 npj

**约束内化 / RL / 引导**：DDPO arXiv:2305.13301 · DRaFT arXiv:2309.17400 · AlignProp arXiv:2310.03739 · Diffusion-DPO arXiv:2311.12908 · GFlowNet arXiv:2106.04399 · SVDD arXiv:2408.08252 · CFG arXiv:2207.12598 · MatInvent arXiv:2511.03112 · CrystalGym arXiv:2509.23156 · OMatG-IRL arXiv:2602.00424 · 自适应约束引导 arXiv:2604.13354

**LLM 内化 / 推理 / 测试时扩展 / 涌现**：内化CoT arXiv:2405.14838 · STaR arXiv:2203.14465 · DeepSeek-R1 arXiv:2501.12948 · Coconut arXiv:2412.06769 · DoT arXiv:2402.07754 · LLaDA arXiv:2502.09992 · d1 arXiv:2504.12216 · **Inference-Time Scaling for Diffusion arXiv:2501.09732** · Classical Search arXiv:2505.23614 · FK steering arXiv:2511.09216 · Proteina-Complexa(ICLR 2026) · AMix-1 arXiv:2507.08920 · Self-Refine arXiv:2303.17651 · Emergent Abilities arXiv:2206.07682 · Mirage arXiv:2304.15004

**筛选/批评/基准**：SMACT(github WMD-group) · S.U.N.(MatterGen) · MatBench Discovery arXiv:2308.14920 · LeMat-GenBench arXiv:2512.04562 · Cheetham&Seshadri Chem.Mater.2024 · Leeman/Palgrave PRX Energy 2024 · Szymanski&Bartel arXiv:2501.02144 / Mater.Horiz.2026 · A-Lab Nature 2023(+2026更正)

---

*本报告与计划为研究规划用途；所有数值在引用进正式论文前应回到一手文献全文核验（调研环境无法逐字抓取学术 PDF）。*
