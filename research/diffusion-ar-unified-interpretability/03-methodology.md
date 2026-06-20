# 03 · Methodology

按三个 RQ 给出具体的度量、模型与算法。核心是把"换基算子"和"基差异"做成可计算的对象。

## 通用设置

- **匹配的轨迹**:对齐前需定义两边的"对应位置"。
  - AR:沿 position / 生成步 `t = 1..L`。
  - DLM:沿 denoising-time / unmasking 步;每一步已揭示的 token 集合 `S_t`。
  - 对齐协议:在**相同的 (prefix/已揭示集合, 待预测位置)** 条件下取双方的隐表征,保证比较的是同一条件分布下的内部状态。
- **取表征的层**:逐层扫(early / mid / late),不预设只看某一层——已知 diffusion 早层冗余多,层选择是变量不是常数。
- **激活收集**:同一批 prompt / 文本,固定 tokenizer 对齐(若两模型 tokenizer 不同,需先做 token-level 对齐或选同 tokenizer 的模型对)。

## RQ1 — 分布层面功能对齐

目标:在功能层面确认"内核"是否同一。

- 构造一组 partial-masking 上下文 `c`(对 AR 等价于 prefix + 询问下一 token;对 DLM 是任意 masking)。
- 度量 DLM 与 AR 在 `c` 下对目标位置的预测分布 `p_DLM(· | c)` vs `p_AR(· | c)`:
  - symmetric KL / JS divergence;
  - top-k agreement、argmax agreement;
  - 校准曲线(是否系统性偏置)。
- 控制:用两个**同范式**模型的距离作为"同一内核"的下界参照(范式内差异),
  跨范式距离若与之同量级 → 强支持同一内核。

## RQ2 — 机制层面换基(核心)

目标:找换基算子 `T`,量化它的复杂度(线性/正交 vs 非线性)与共享子空间占比。

### (a) 线性 / 正交对齐 — 测"强版本"

- **Orthogonal Procrustes**:解 `min_T ||H_AR - H_DLM T||_F, s.t. T^T T = I`,报告对齐残差。
- **CKA**(linear & RBF):范式间表征相似度,逐层 × 逐轨迹位置成热图。
- **岭回归 / 低秩线性映射**:允许非正交线性,看秩-误差曲线(rank–distortion);
  若低秩即可对齐 → 线性换基成立。

### (b) Crosscoder — 测"共享 vs 特有"占比

- 训练一个 **crosscoder**,联合重建 `h_AR` 与 `h_DLM`:共享 latent + 模型特有 decoder。
- 度量:共享特征解释的方差比例 / 重建占比 = "共享内核"的定量估计;
  model-specific 特征 = "基差异"的具体载体,做特征级解释(它们对应什么语言学/位置/频率结构)。
- 复用 **DLM-Scope** 的 Top-K SAE 基建迁移到 crosscoder 设置。

### (c) 非线性对照

- 训练一个小 MLP 作为 `T`,与线性 `T` 比对齐残差。
- 判据:若 MLP 显著优于线性且差距随深度增大 → "基"框架退化为非线性(部分成功)。

### 输出

- 一张 (层 × 轨迹位置) 的"共享比例 / 对齐残差"热图;
- `T` 的复杂度等级判定(正交 / 低秩线性 / 非线性);
- model-specific 特征清单 + 解释 = "基到底差在哪"。

## RQ3 — 可学基 + 直度正则(邪修加速)

目标:检验"内核简单"与"ODE 路径直"能否在同一个基里兼得。

### 设置(连续 / latent,字面成立的唯一干净场景)

- 在文本 latent(冻结或联合训练的 autoencoder 给出连续表征)上做 flow matching。
- 把**基**参数化为可学正交 / 可逆变换 `B`(或可学 autoencoder 的 latent 几何)。

### 联合目标

```
L = L_recon(kernel)                      # 重建共享内核 / 数据似然
  + λ_straight · L_straight(PF-ODE)      # 路径直度:路径曲率 or NFE-to-quality
  + λ_couple  · L_coupling               # OT / rectified coupling,直度的真正抓手
```

- `L_straight`:沿 PF-ODE 估计路径曲率(相邻速度场夹角)或固定质量下的 NFE。
- `L_coupling`:minibatch-OT 或 rectified-flow 式重耦合——提醒:**直度主要由 coupling 决定,基是辅助**。
- 关键消融:固定 coupling 只学基 / 固定基只学 coupling / 联合 —— 验证"联合是否优于分头"。

### Baselines

DCTdiff(固定频率基)、blurring diffusion、standard latent flow matching、OT-CFM、rectified flow。

### 风险与前提

- 离散 token 无原生 ODE → 必须经连续松弛;松弛质量本身是混杂因子,需先单独验证 latent 的重建/生成质量达标。
- 先在 **toy 连续分布**(已知最优传输)验证方法,再上 latent-text。
