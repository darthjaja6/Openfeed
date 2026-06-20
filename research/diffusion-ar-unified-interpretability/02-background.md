# 02 · Background & Related Work

本研究的 hypothesis(同一内核、不同基)并非凭空——它是两条已知结果的推广。
理清这些边界,才能说清"我们做的新东西到底新在哪"。

## A. 分布层面的等价性已经是定理(语言)

masked / absorbing diffusion 与 **any-order autoregression (AO-ARM)** 被证明等价:

- **Hoogeboom et al. 2021 (ARDM)**:absorbing diffusion ≡ order-agnostic AR。
- **Ou et al. 2024 / Zheng et al. 2025**:MDM 的训练目标可精确写成"对所有 ordering 求期望",
  noise schedule 恰好定义 ordering 的分布(Prop 3.1 / 3.2)。
- **Masked Diffusion Models are Secretly Learned-Order Autoregressive Models** (2511.19152):标题即结论。
- UT Austin talk: *Are Diffusion and Autoregression Truly Different? Insights from Masked Diffusion Models* (2026)。

**含义**:这里的"基"就是**因子化顺序**。AR 固定一个 order 展开联合分布,diffusion 在所有 order 上混合。
同一套条件分布(= 内核),不同展开顺序(= 基)。
→ 这是本 hypothesis 的字面证明,但**只在分布层面**。本研究补的是机制层面。

## B. 连续域的"换基"是近似(图像,作类比参照)

- **Diffusion is spectral autoregression (Dieleman 2024)**:像素空间 diffusion ≈ **频率基(Fourier)**下的近似 AR
  (高噪声先定低频,去噪逐步补高频)。
- **反方证据(划定边界)**:
  - Milanfar 反驳"diffusion 并不*只是*谱自回归"——换基等价是**有损近似**,非精确同构;
  - coarse-to-fine / 频率故事**对 audio 波形不成立**。

**含义**:连续域里"基 = 频率",但等价有损、依赖数据谱结构。提醒我们:"基"在图像(Fourier,线性)
与语言(order,组合)里是**两种不同的数学对象**,统一陈述时必须小心。

## C. 机制 / 表征层面的对比(本研究的直接上游)

- **Skip to the Good Part (2603.07475)**:diffusion 目标产生更分层、早层冗余多、recency bias 弱的结构;
  AR 产生深度强耦合的表征。**关键**:AR 初始化的 DLM 保留 AR 式表征动态(initialization bias 持续)。
  → 既是"结构不同"也是"内核部分共享"的双向证据。
- **Diffusion vs. AR: A Text Embedding Perspective (2505.15045, EMNLP 2025)**:两范式表征有意义地不同,各有任务优势。
- **Scaling DLMs via Adaptation from AR (2410.17891)**:AR→diffusion 可适配,本身即"知识大体共享、可迁移"的强证据。

## D. 验证"同一内核"的工具(方法学,目前的空白)

- **SAEs Reveal Universal Feature Spaces Across LLMs (2410.06981)**:不同 AR LLM 的 SAE 特征空间在旋转不变变换下相似。
  方法可直接搬到 DLM vs AR。
- **Sparse Crosscoders for Model Diffing (Anthropic 2024)** + **Cross-Architecture Model Diffing (2602.11729)**:
  联合建模两个激活空间,学"共享特征 + 模型特有解码权重"。
  → 几乎为本问题量身定做:**共享部分 = 内核,model-specific = 基差异**。
  但尚无 DLM↔AR 的 crosscoder 工作——**这是空白点之一**。
- **DLM-Scope (2602.05859)**:首个 DLM 的 SAE 机制可解释性框架(Dream-7B / LLaDA-8B),
  支持 diffusion-time steering 与跨 remasking 顺序的表征追踪。本研究 RQ2 的现成基础设施。

## E. 加速线:把"基"当可优化变量(RQ3 的上游)

- **DCTdiff (2412.15032)**:在 DCT(频率)基里做 diffusion,报告效率提升——本提案在图像上的实例。
- **Inverse Heat Dissipation / Blurring Diffusion (2206.13397)**:在频率基里设计前向过程。
- **Improving the Diffusability of Autoencoders (2502.14831)**:设计 latent 基让 diffusion 更好学。
- **Decomposable Flow Matching (2506.19839)**:多尺度 / 分解式 flow matching。
- **直度线(binding constraint)**:Rectified Flow、OT-CFM(minibatch optimal transport CFM)、
  consistency / flow-map models——少步 marching 的瓶颈是 **PF-ODE 路径直度**,而直度主要由 **coupling** 决定,**不是基**。

**关键缺口**:基(DCTdiff / blurring)与直度(rectified flow / OT-CFM)被**分头攻**;
"让内核简单 *且* 让路径直"的**联合 (基, coupling) 优化**没人系统做。RQ3 切的就是这里。
