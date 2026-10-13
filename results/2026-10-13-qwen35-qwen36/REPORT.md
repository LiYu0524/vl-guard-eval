# 2026-10-13 Qwen 3.5 / 3.6 扩展评测

Qwen3.5-27b 的整轨迹／步骤 ACC 为 **77.32%／85.67%**；Qwen3.6-27b 为 **85.33%／88.68%**。Qwen3.5 完成有界收尾后保留 3 个 unknown，Qwen3.6 有效二分类覆盖为 100%。以下为正式冻结结果，pilot 与格式诊断不参与。

## 四模型成绩

| 模型 | 粒度 | ACC | 95% ACC 区间 | 有效覆盖 | TP | FN | FP | TN |
|---|---|---:|---|---:|---:|---:|---:|---:|
| qwen3.8-27b | whole | 90.69% | [89.00%, 92.25%] | 100.00% | 3,125 | 581 | 274 | 5,200 |
| qwen3.8-27b | step | 96.13% | [95.18%, 97.01%] | 100.00% | 3,414 | 292 | 63 | 5,411 |
| deepseek-v4.1-flash | whole | 91.01% | [89.12%, 92.77%] | 99.64% | 3,488 | 214 | 608 | 4,837 |
| deepseek-v4.1-flash | step | 96.11% | [95.19%, 96.95%] | 99.88% | 3,580 | 123 | 234 | 5,232 |
| qwen3.5-27b | whole | 77.32% | [74.75%, 80.07%] | 99.98% | 1,717 | 1,989 | 93 | 5,379 |
| qwen3.5-27b | step | 85.67% | [83.78%, 87.59%] | 99.99% | 2,419 | 1,286 | 29 | 5,445 |
| qwen3.6-27b | whole | 85.33% | [83.33%, 87.25%] | 100.00% | 2,554 | 1,152 | 195 | 5,279 |
| qwen3.6-27b | step | 88.68% | [87.23%, 90.27%] | 100.00% | 2,695 | 1,011 | 28 | 5,446 |

Qwen3.5 whole 为 9,178 / 9,180 有效（99.9782%），其余 2 个为参考 safe 的 unknown；step 为 9,179 / 9,180 有效（99.9891%），其余 1 个为参考 unsafe 的 unknown。两种模式均无最终错误或缺失。DeepSeek 保留 whole 19 个 unknown、14 个格式错误，step 7 个 unknown、4 个格式错误。ACC 按各模型自身有效二分类分母计算，unknown／错误不补为 safe。

## 配对 ACC 差值

| 左模型 − 右模型 | 粒度 | 差值 | 配对 95% 区间 | 共同有效样本 / case |
|---|---|---:|---|---:|
| qwen3.5-27b − qwen3.8-27b | whole | -13.37 pp | [-15.79, -11.08] pp | 9,178 / 233 |
| qwen3.5-27b − qwen3.8-27b | step | -10.47 pp | [-12.13, -8.86] pp | 9,179 / 233 |
| qwen3.6-27b − qwen3.8-27b | whole | -5.36 pp | [-6.54, -4.17] pp | 9,180 / 233 |
| qwen3.6-27b − qwen3.8-27b | step | -7.45 pp | [-8.74, -6.19] pp | 9,180 / 233 |
| qwen3.5-27b − qwen3.6-27b | whole | -8.02 pp | [-9.50, -6.63] pp | 9,178 / 233 |
| qwen3.5-27b − qwen3.6-27b | step | -3.01 pp | [-3.70, -2.34] pp | 9,179 / 233 |

配对差值只使用双方共同有效样本，不能直接用上表各自 ACC 相减代替。例如 Qwen3.5 对 Qwen3.8 的 step 配对差值为 −10.47 pp；独立 ACC 相减约为 −10.46 pp，分母不同。新增两模型相对 Qwen3.8 的 ACC 区间差值均低于 0。

新增模型的误报总体较少，同时漏报较多。whole FN 从 Qwen3.8 的 581 增至 Qwen3.6 的 1,152、Qwen3.5 的 1,989；step FN 分别为 292、1,011、1,286。因此，较低 FP 不能单独证明风险检测更好。

## 执行与有界收尾

复用冻结的 9,180 条轨迹、3,706 个正例、5,474 个负例和 233 个 case。whole 使用完整已记录轨迹；step 对正例评估首个未授权待执行调用，对负例评估既定 control 调用，排除该调用结果与后续事件。标签和 selected_step 未重选，输入未截断。正式请求采用 temperature=0、max_tokens=512、JSON mode；Qwen enable_thinking=false，初始并发 256，timeout=240 秒。

| 模型 / 阶段 | 开始（UTC） | 结束（UTC） | Attempts | 提交样本 | 新有效预测 | 累积有效 | 未解决 unknown / 错误 |
|---|---|---|---:|---:|---:|---:|---:|
| qwen3.5-27b / full | 13:55:53 | 15:26:39 | 25,036 | 18,360 | 16,822 | 16,822 | 5 / 1533 |
| qwen3.5-27b / cleanup1 | 15:27:37 | 15:35:28 | 1,899 | 1,538 | 1,485 | 18,307 | 3 / 50 |
| qwen3.5-27b / cleanup2 | 15:37:44 | 15:41:52 | 82 | 53 | 50 | 18,357 | 3 / 0 |
| qwen3.5-27b / cleanup3 | 15:42:24 | 15:42:32 | 3 | 3 | 0 | 18,357 | 3 / 0 |
| qwen3.6-27b / full | 13:55:53 | 14:51:35 | 20,586 | 18,360 | 17,663 | 17,663 | 0 / 697 |
| qwen3.6-27b / cleanup1 | 14:54:29 | 15:01:21 | 700 | 697 | 697 | 18,360 | 0 / 0 |

以上阶段均在 2026-10-13 UTC 执行，精确时间及逐阶段状态计数见 [run_manifest.json](run_manifest.json)。Qwen3.5 正式 attempt 共 27,020 行：18,357 predicted、5,917 request_error、2,731 parse_error、14 unknown、1 output_truncated；Qwen3.6 共 21,286 行：18,360 predicted、2,924 request_error、2 parse_error。每样本按原始行序取最后一个有效 safe／unsafe，保留全部历史 attempt。

Qwen3.5 的收尾预算在 cleanup1 运行期间、cleanup2／cleanup3 决策和最终 ACC 前固定：全量首轮后最多 3 个 resume 阶段，每阶段并发 64、每未有效样本最多 3 次 attempt，跳过已有效样本，generation 参数不变。3 次是每阶段上限；含全量首轮的理论单样本总上限为 12 次，实际最大为 8 次。提前全量有效则停止；否则预算后按真实覆盖发布。三个 cleanup 阶段用尽后保留上述 3 个 unknown，不再继续请求。

Qwen3.5 曾返回不符合仅含 decision、reason 要求的格式；finish_reason=stop 也不保证可解析。格式错误与输出截断记录在正式 attempt 历史中，不能当作 safe。两模型最初的验证 pilot 和另行格式诊断均未进入公开 attempt 或统计，诊断结果也未替代正式预测。

## 旧 Guard 步骤对照

| 检测配置 | 步骤 ACC |
|---|---:|
| TS-Guard/native_block | 60.66% |
| TS-Guard/harmfulness_block | 60.98% |
| TPR/high_only | 60.25% |
| TPR/incl_borderline | 62.33% |

四行仍是原冻结 selected_step 的历史配置，未新增整轨迹成绩。历史及时拦截率见 [旧结果快照](../2026-10-13/REPORT.md)：首次 block 在首个未授权调用之前或当次调用；此次追加未重跑该指标，离线分类 ACC 不能代替在线拦截实验。

## 复现与限制

完整指标见 [comparison.csv](comparison.csv)、[summary.json](summary.json) 与 [paired_differences.csv](paired_differences.csv)：12 行比较、28 组配对、140 行配对指标。采用 case-cluster percentile bootstrap，1,000 次、seed=20261013、95% 置信度；按 case 重采样并保留其轨迹。显式指定本快照 summary 的复验通过；旧两模型、四个 baseline 和旧 10 组配对统计逐对象保持一致，旧快照未修改。

参考来自历史 GLM-5.3-flash 机器标注，并非人工金标准。部分授权边界有争议，未通过人工裁决改标签。旧 Guard 原配置与新模型专项提示不同；结论限于记录的授权信息、这些检测配置和冻结参考口径。关闭 thinking 的固定提示成绩不代表最大推理能力。AgentDoG 1.5 未评测。

公开记录复用既有匿名 ID，只保留冻结标签、决定、数值 usage 和 attempt 元数据，保持原始行序与一基 source_attempt_index。原轨迹、回答正文、reasoning、错误正文、原 ID、fingerprint、run ID、凭据和映射均不公开；详见 [publication_manifest.json](publication_manifest.json)。
