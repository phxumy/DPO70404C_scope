# 给 AWG 电脑 Codex 的完整指令

请在 `D:\labradPy3_72qubit\DataTakingEclipse` 中新建一次独立的 **M=20 纵场时间/幅度精度扫描**。先做 build-only dry-run，不开 AWG Output，不启动 trigger。不得复用旧 run_id 或旧 batch_id。

## 1. 先查清真实实现

1. 查找 `timingLagMeas` 从请求值到最终 q9 Z SRAM 的完整路径，确认它是 Decimal/连续 Fourier 重采样、整 sample 平移，还是其他实现。
2. 查找当前纵场的真实 `amp` 变量、基准值 `A0`、单位、允许范围、是加法参数还是比例系数，以及它影响哪个通道/门/时间段。
3. 本次用户给出的幅度数字 `0.01, 0.005, 0.001, 0.0005, 0.0001` 默认解释为 **当前程序 amp 单位中相对 A0 的加法变化量**，不是伏特，也不是百分比。如果代码语义不支持这个解释，不得自行换算；停止并报告 A0、单位和正确 API。
4. 对每个点从最终实际 SRAM 导出 requested/commanded/realized 值、量化误差、SRAM SHA256、与参考 SRAM 不同的 sample 数、最大码值差、RMS/积分差。如果两个请求值产生完全相同的最终 SRAM，明确标记为同一 digital realization，不得声称两者可分。

## 2. 固定的实验对象

- 保持正式实验的 q9/q10/c910、CZenv8_flat、M=20 和其他参数不变。
- 首轮只观察 q9 Z 的 **Layer 10 完整局部纵场**，不需要抓 20 层。
- AUX marker 必须在所有点完全相同：rise sample 固定，每个 shot 恰好一个上升沿，不随 timingLagMeas 或 amp 改变。
- 每个参数点仍是一条独立完整 M=20 序列；禁止把不同参数点拼成一条模拟波形。
- warmup=0，discard=[]。上传/切换期间 marker 必须保持低电平。

## 3. 时间精度：两个独立 batch

为了做对称测量，如果当前 Registry 基准是 0 ns 且负绝对值不合法，使用 `T0=0.25 ns`（旧实验已在 0–0.5 ns 内运行，但仍要由本次 dry-run 验证）。保存同时保存 `T0`、请求绝对值、实现绝对值和相对 T0 的实现偏移。

### timing_precision_coarse

- signed delta ns: `[-0.05, -0.01, 0, +0.01, +0.05]`
- 20 shots/点，总帧数 100。
- 4 个 cycle，每 cycle 每点 5 shots。
- 每个 cycle 用固定 random seed 生成可复现的平衡顺序，正负号交替，不得先连续发完某点 20 发再换点。

### timing_precision_fine

- signed delta ns: `[-0.005, -0.001, 0, +0.001, +0.005]`
- 50 shots/点，总帧数 250。
- 10 个 cycle，每 cycle 每点 5 shots；同样用可复现平衡顺序。

## 4. 幅度精度：两个独立 batch

以真实当前幅度 `A0` 为中心。所有变化均为程序原生 amp 单位中的加法变化：`A=A0+ΔA`。在 dry-run 中检查正负点均不越界。只改 amp，timingLagMeas 和其他参数必须不变。

### amplitude_precision_coarse

- signed delta: `[-0.01, -0.005, 0, +0.005, +0.01]`
- 20 shots/点，总帧数 100。
- 4 cycles × 5 shots/点/cycle，平衡交错顺序。

### amplitude_precision_fine

- signed delta: `[-0.001, -0.0005, -0.0001, 0, +0.0001, +0.0005, +0.001]`
- 50 shots/点，总帧数 350。
- 10 cycles × 5 shots/点/cycle，平衡交错顺序。

## 5. AWG 执行和手动握手

四个 batch 必须分开运行，一次只让示波器武装一个 batch。每个 batch：

1. AWG build/upload 第一个 execution block，但不运行。
2. 用户在示波器电脑运行对应 arm 命令。
3. 示波器终端打印 `SCOPE READY <batch_id>` 后，AWG 端只接受精确字符串 `START <batch_id>`。
4. 按 contract 的 frame 顺序自动上传并运行各 microblock；每 shot 恰好一个 marker rise。
5. 异常时 `finally` 关闭 q9 输出，保存已执行 frame/block 日志，不用后续帧补齐失败 batch。

## 6. 必须交给示波器电脑的文件

生成：

```text
precision_scope_contract.json
HANDOFF_PRECISION_TO_SCOPE_CODEX.md
precision_scan_plan.json
precision_scan_summary.csv
```

`precision_scope_contract.json` 顶层必须与现有采集合同兼容，并设置：

```json
{
  "schema_version": 2,
  "contract_status": "ready_for_scope_capture",
  "run_id": "<fresh-run-id>",
  "awg_sample_rate_hz": 2000000000.0,
  "awg_sram_samples": 32768,
  "awg_period_s": 0.000016384,
  "marker_rise_sample": 200,
  "marker_rising_edges_per_period": 1,
  "scope_resource": "TCPIP0::192.168.1.7::inst0::INSTR",
  "scope_defaults": "<保留原有完整字段>",
  "prior_path_delay": "<保留原有完整字段>",
  "batches": {}
}
```

每个 batch 必须有：

```text
batch_id
experiment = timing_precision 或 amplitude_precision
target_layers = [10]
total_frames
shots_per_point
warmup_shots = 0
discard_frames = []
marker_fixed_across_points = true
digital_center_ns
digital_coverage_ns
scope_span_ns = 40.0（若最终波形不能完整覆盖则报告并改宽）
confirmation = START <batch_id>
required_requested_magnitudes
analysis.reference_point_id
analysis.active_roi_digital_ns
analysis.derivative_smoothing_points = 7
analysis.bootstrap_replicates = 2000
analysis.bootstrap_block_shots = 5
points
```

每个 point 用 `frame_ranges_1based` 表示它在多个 cycle 中的所有帧段；所有 point 合起来必须无重叠地恰好覆盖 `1..total_frames`。每个 point 还必须有：

```text
point_id
frame_ranges_1based
marker_rising_edges_per_period = 1
marker_sha256
final_sram_sha256
target_start_ns
target_end_ns
cycle/occurrence映射
```

timing point 额外字段（小数全部使用 JSON 字符串）：

```text
requested_delta_ns
requested_timing_lag_absolute_ns
realized_timing_lag_absolute_ns
realized_delta_ns
realized_timing_lag_offset_ns   # 与 realized_delta_ns 相同，供命令校正图使用
quantization_error_ns
changed_sample_count_vs_reference
max_abs_code_difference_vs_reference
```

amplitude point 额外字段（小数全部使用 JSON 字符串）：

```text
amp_unit
base_amp
requested_amp_delta
requested_amp_absolute
realized_amp_delta
realized_amp_absolute
quantization_error_amp
changed_sample_count_vs_reference
max_abs_code_difference_vs_reference
```

`HANDOFF_PRECISION_TO_SCOPE_CODEX.md` 和最终回复必须完整列出：4 个 batch key/ID、点顺序、每点所有 frame ranges、T0/A0/单位、requested/realized 值、SRAM 是否唯一、marker hash/边沿数、固定窗口、精确 START 字符串、AWG 运行命令、异常停止行为和所有未解决问题。

最后明确回复用户：

```text
请把“给示波器电脑Codex的交接消息”完整复制到示波器电脑原来的Codex对话中，然后让它用 precision_scope.py 验证合同并逐个武装四个 batch。
```
