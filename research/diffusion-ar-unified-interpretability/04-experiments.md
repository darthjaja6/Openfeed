# 04 · Experiment Plan & Milestones

## 模型对

| 角色 | 候选 | 用途 |
|---|---|---|
| DLM | LLaDA-8B、Dream-7B | 主体 |
| AR | LLaMA 量级、Dream 的 AR base | 对照 |
| **同源对** | Dream(AR base) ↔ Dream(diffusion) | 隔离 initialization bias |
| **独立对** | LLaDA ↔ 独立训练的 AR | 隔离目标函数本身效应 |

> tokenizer 不一致时,优先选同 tokenizer 的对,或先做 token 级对齐。

## 数据

- 通用文本(预训练分布的子样本)用于激活收集与对齐。
- 结构化探针集:不同句法/位置/长程依赖的受控样本,用于解释 model-specific 特征。
- RQ1 的 partial-masking 上下文集合:覆盖不同 masking 比例与位置模式。

## 阶段与里程碑

### Phase 0 — 基建(1–2 周)
- [ ] 激活收集管线:对齐协议(相同条件下取 DLM/AR 隐表征)。
- [ ] 复用 DLM-Scope 的 SAE/crosscoder 训练代码。
- [ ] 健全性检查:同范式自比对(范式内距离基线)。

### Phase 1 — RQ1 功能对齐(1 周)
- [ ] DLM vs AR 预测分布 KL/JS、agreement、校准。
- [ ] 里程碑:跨范式距离 vs 范式内距离的对比结论。

### Phase 2 — RQ2 换基(核心,3–4 周)
- [ ] Procrustes / CKA / 低秩线性 rank–distortion 曲线。
- [ ] crosscoder 共享 vs 特有占比;model-specific 特征解释。
- [ ] 非线性 MLP 对照。
- [ ] 里程碑:`T` 复杂度判定 + (层×位置) 共享热图 + "基差异载体"清单。

### Phase 3 — RQ3 可学基加速(探索,4–6 周)
- [ ] toy 连续分布上验证联合目标(基 × coupling × 直度)。
- [ ] latent-text 上验证;与 DCTdiff / OT-CFM / rectified flow 比 NFE-to-quality。
- [ ] 消融:固定基 / 固定 coupling / 联合。
- [ ] 里程碑:固定质量下 NFE 或训练步数的相对降幅。

## 关键消融与对照(防自欺)

- **范式内基线**:同范式两模型的对齐残差 = "同一内核"的经验下界。
- **随机/打乱基线**:打乱轨迹对应关系后对齐残差应显著变差。
- **同源 vs 独立**:若仅同源对可线性对齐 → 共享来自 initialization 而非范式本质(重要的负面结论)。
- **tokenizer 混杂**:同 tokenizer vs 跨 tokenizer 结果是否一致。

## 交付物

1. RQ2 的换基复杂度判定(强/部分/证伪)——**主结论**。
2. (层 × 轨迹位置) 共享比例热图 + model-specific 特征解释。
3. RQ3 的可学基加速概念验证(若成立)。
4. 可复现代码 + 文档。
