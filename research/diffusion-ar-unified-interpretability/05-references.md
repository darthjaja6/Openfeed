# 05 · References

> 链接为 arXiv / 官方页面。部分为 2025–2026 近期工作,引用时请核对最终版本号。

## 等价性:diffusion ≡ any-order autoregression(语言)

- Hoogeboom et al. 2021. *Autoregressive Diffusion Models (ARDM)*. (absorbing diffusion ≡ order-agnostic AR)
- Ou et al. 2024 / Zheng et al. 2025. MDM 目标 = 对 ordering 求期望(Prop 3.1/3.2)。
- *Masked Diffusion Models are Secretly Learned-Order Autoregressive Models.* arXiv:2511.19152 — https://arxiv.org/pdf/2511.19152
- *Autoregressive Models Rival Diffusion at Any-Order Generation.* arXiv:2601.13228 — https://arxiv.org/html/2601.13228v1
- UT Austin talk: *Are Diffusion and Autoregression Truly Different? Insights from Masked Diffusion Models* (2026) — https://ai.utexas.edu/events/2026-02-06/

## 谱自回归 / 换基(图像,类比参照)

- Dieleman, S. 2024. *Diffusion is spectral autoregression.* — https://sander.ai/2024/09/02/spectral-autoregression.html
- Milanfar, P. 2024. pushback("not *just* spectral autoregression")— https://x.com/docmilanfar/status/1830714858763100214

## 机制 / 表征层面对比

- *Skip to the Good Part: Representation Structure & Inference-Time Layer Skipping in Diffusion vs. AR LLMs.* arXiv:2603.07475 — https://arxiv.org/pdf/2603.07475
- *Diffusion vs. Autoregressive Language Models: A Text Embedding Perspective.* arXiv:2505.15045 (EMNLP 2025) — https://arxiv.org/abs/2505.15045
- *Scaling Diffusion Language Models via Adaptation from Autoregressive Models.* arXiv:2410.17891 — https://arxiv.org/html/2410.17891v2

## 可解释性工具:SAE / crosscoder / 普适性

- *Sparse Autoencoders Reveal Universal Feature Spaces Across LLMs.* arXiv:2410.06981 — https://arxiv.org/abs/2410.06981
- *Sparse Crosscoders for Cross-Layer Features and Model Diffing.* Anthropic, 2024 — https://transformer-circuits.pub/2024/crosscoders/index.html
- *Cross-Architecture Model Diffing with Crosscoders.* arXiv:2602.11729 — https://arxiv.org/pdf/2602.11729
- *DLM-Scope: Mechanistic Interpretability of Diffusion Language Models via Sparse Autoencoders.* arXiv:2602.05859 — https://arxiv.org/html/2602.05859

## 加速:可学基 / 直度 / flow matching

- *DCTdiff: Intriguing Properties of Image Generative Modeling in the DCT Space.* arXiv:2412.15032 — https://arxiv.org/pdf/2412.15032
- *Generative Modelling With Inverse Heat Dissipation (Blurring Diffusion).* arXiv:2206.13397 — https://arxiv.org/pdf/2206.13397
- *Improving the Diffusability of Autoencoders.* arXiv:2502.14831 — https://arxiv.org/html/2502.14831
- *Improving Progressive Generation with Decomposable Flow Matching.* arXiv:2506.19839 — https://arxiv.org/pdf/2506.19839
- Lipman et al. 2022. *Flow Matching for Generative Modeling.*
- Liu et al. 2022. *Rectified Flow.*
- Tong et al. 2023. *Conditional Flow Matching / minibatch OT-CFM.*

## 背景:DLM 模型

- *Large Language Diffusion Models (LLaDA).* — https://openreview.net/forum?id=KnqiC0znVF
- *Dream 7B: Diffusion Large Language Models.* arXiv:2508.15487 — https://arxiv.org/pdf/2508.15487

## 理论母题

- Huh, Isola et al. 2024. *The Platonic Representation Hypothesis.* arXiv:2405.07987 — https://arxiv.org/abs/2405.07987
- *Back into Plato's Cave: Examining Cross-modal Representational Convergence at Scale.* arXiv:2604.18572 — https://arxiv.org/html/2604.18572
