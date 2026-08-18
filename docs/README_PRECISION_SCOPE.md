# Timing / amplitude precision capture on DPO70404C

`precision_scope.py` reuses the verified FastFrame acquisition path in
`formal_longitudinal_scope.py`. It adds dynamic precision-batch selection and a
fixed-template analysis that estimates timing and amplitude response together.

The script intentionally has no usable built-in scan contract. The AWG computer must
first produce a fresh `precision_scope_contract.json` from its build-only dry-run. A
template or stale batch ID is rejected before the scope is opened.

## Scientific guardrails

- Timing and amplitude are separate batches.
- AUX marker arrays must be identical at every point and contain one rising edge per
  waveform period.
- Every requested value, command value, realized value, final SRAM hash, and frame
  range comes from the AWG dry-run. The scope code never guesses quantization.
- Raw waveforms remain AUX-aligned. No cross-correlation or per-frame shift is used.
- The analysis uses one zero-delta reference template and fits the fixed columns
  `[template derivative, template amplitude, constant]`. The reported timing
  coefficient is a measurement; it is not used to move saved waveforms.
- Confidence intervals resample shots in circular blocks. Scope time samples are not
  treated as independent repeats.
- A batch-local sub-sample estimate is not an absolute timebase-accuracy claim.

## Commands

The global `--config` option must appear before the subcommand.

```powershell
$cfg = Resolve-Path .\configs\precision_scope_contract.local.json
$cal = Resolve-Path .\data\scope_runs\formal-longitudinal-20260815_224030-9e200ecf\calibration\formal-longitudinal-20260816_110341-79304963-gatelen-L10-b01\attempt_001\scope_path_calibration.json

.\.venv\Scripts\python.exe .\precision_scope.py --config $cfg validate
```

Configure and query one batch without arming or consuming a trigger:

```powershell
.\.venv\Scripts\python.exe .\precision_scope.py --config $cfg preflight --batch <batch_key> --calibration-file $cal
```

Arm exactly one FastFrame batch:

```powershell
.\.venv\Scripts\python.exe .\precision_scope.py --config $cfg arm --batch <batch_key> --calibration-file $cal
```

If the AWG dry-run predicts that the amplitude coarse scan can exceed the calibrated
20 mV/div range, add an explicit safer setting such as
`--vertical-scale-mv 50 --vertical-offset-v 0.244`. Every override is queried back and
saved. Never use Autoset.

Wait until the terminal prints `SCOPE READY`, then run the matching AWG batch and enter
the exact `START <batch_id>` string printed by the scope program. Do not arm two batches
at once.

After capture completion and AWG-log review:

```powershell
.\.venv\Scripts\python.exe .\precision_scope.py analyze --attempt <absolute_attempt_directory>
```

The analysis is saved below the attempt in `precision_analysis/`:

- `point_metrics.csv`: per-point fixed-template estimates and shot repeatability.
- `resolution_table.csv`: each requested magnitude, 95% block-bootstrap interval, and
  whether it is resolved from zero in this batch.
- `precision_analysis.json`: global response slope, interpretation, and file map.
- `precision_summary.png`: raw AUX-aligned means, reference differences, response
  line, and residuals.
- `xzero_phase_histogram.png`: actual sub-sample trigger-phase coverage.
- `bootstrap_point_estimates.npz`: bootstrap distributions used for the intervals.

## Expected batch split

- `timing_precision_coarse`: signed 0, 10 ps, and 50 ps; 20 shots/point.
- `timing_precision_fine`: signed 0, 1 ps, and 5 ps; 50 shots/point.
- `amplitude_precision_coarse`: signed 0, 0.005, and 0.01 in the program's native
  `amp` unit; 20 shots/point.
- `amplitude_precision_fine`: signed 0, 0.0001, 0.0005, and 0.001 in that same unit;
  50 shots/point.

Signed points are centered on a safe nonzero reference. For timing, use a nominal
`timingLagMeas` of 0.25 ns if negative absolute values are not legal. For amplitude,
the listed values are additive changes in the current program `amp` parameter; they
must not be silently reinterpreted as volts or percentages.

The fine scans are pilots. With the roughly 50 ps single-shot timing scatter seen in
the previous data, 50 shots cannot by themselves prove that two neighboring 1 ps
commands are separately resolved. Their purpose is to measure the actual noise floor,
digital realization, monotonicity, and the amount of data needed for a stronger claim.
