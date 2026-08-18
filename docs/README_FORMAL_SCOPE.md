# DPO70404C formal longitudinal-scan capture

This scope-side program consumes the AWG dry-run handoff for run
`formal-longitudinal-20260815_224030-9e200ecf`. It never controls the AWG.
The operator is the manual handshake between the two computers.

## What the program enforces

- CH3: 50 ohm, DC coupling, full 4 GHz bandwidth.
- Trigger: AUX rising edge, 1.4 V, Normal mode.
- FastAcq off; real-time Sample acquisition.
- Formal batches: 25 GS/s, 1000 points, 40 ps/point, 40 ns per frame.
- FastFrame: `SEQUENCE FIRST`, `SUMFRAME NONE`, exactly 120 frames.
- One arm operation for the whole batch; no per-shot rearming.
- Frame-to-point assignment comes only from the handed-off 1-based frame map.
- Every frame retains raw ADC codes, its own FastFrame XZERO, its absolute
  timestamp, and the full waveform preamble.
- No cross-correlation or waveform-dependent free time shift is used.

The FastFrame map remains provisional until the AWG execution log confirms that
all six points emitted exactly 20 legal marker edges. A count of 120 on the scope
alone cannot exclude one missing legal trigger being replaced by one noise edge.

## Files

- `formal_longitudinal_scope.py`: audit, path preview, and formal capture.
- `configs/formal_scope_batches.json`: local snapshot of the AWG handoff.
- `data/scope_runs/<run_id>/...`: append-only capture attempts; old attempts are not
  overwritten or combined with a later attempt.

## 1. Read-only audit

Run this before touching the AWG:

```powershell
.\.venv\Scripts\python.exe formal_longitudinal_scope.py audit
```

Expected current instrument identity:

```text
TEKTRONIX,DPO70404C,C600752,CF:91.1CT FV:10.9.1 Build 16
```

The following checks the exact 120-frame settings without arming or consuming a
trigger:

```powershell
.\.venv\Scripts\python.exe formal_longitudinal_scope.py preflight --batch gatelen
```

The following optional hardware smoke test uses three scope-forced triggers. It
does not start or contact the AWG, but it verifies the FastFrame memory, multi-
frame `CURVE?` transfer, reshape, per-frame XZERO, and timestamp commands:

```powershell
.\.venv\Scripts\python.exe formal_longitudinal_scope.py smoke-test
```

## 2. One-time path-delay and vertical preview

The old appended-staircase measurements give a prior differential delay of
about 10.853 ns between the AUX marker path and the CH3 analog path. It is only
an initial guess. The 40 ns formal windows have only a few ns of margin, so run
one separate 80 ns preview before the first formal batch.

On the scope computer:

```powershell
.\.venv\Scripts\python.exe formal_longitudinal_scope.py preview --batch gatelen --shots-per-point 1
```

The command arms a six-frame preview: one shot for each of the six gateLen
points. Wait until it prints `SCOPE READY`. On the AWG computer, run the gateLen
batch with `shots=1`, verify that its console predicts exactly six marker edges,
and enter the exact `START <batch_id>` string requested by the AWG program.

After all six frames arrive, inspect:

```text
data/scope_runs/<run_id>/calibration/<batch_id>/attempt_NNN/path_delay_preview.png
data/scope_runs/<run_id>/calibration/<batch_id>/attempt_NNN/scope_path_calibration.json
```

The calibration JSON is deliberately marked `candidate_requires_visual_review`.
It fits the same programmed tfall feature in all six gateLen points, takes one
median delay, and never refits individual formal frames. Reject it if the six
candidate delays do not identify the same edge or their spread is unexpectedly
large.

## 3. Formal 120-frame captures

Use the reviewed calibration file for every formal batch in the unchanged cable
session. Start only one scope command at a time. Wait for `SCOPE READY`, then run
the matching AWG batch and type the exact AWG confirmation string.

```powershell
$cal = "D:\absolute\path\to\scope_path_calibration.json"

.\.venv\Scripts\python.exe formal_longitudinal_scope.py capture --batch gatelen --calibration-file $cal
.\.venv\Scripts\python.exe formal_longitudinal_scope.py capture --batch timinglag_l1 --calibration-file $cal
.\.venv\Scripts\python.exe formal_longitudinal_scope.py capture --batch timinglag_l10 --calibration-file $cal
.\.venv\Scripts\python.exe formal_longitudinal_scope.py capture --batch timinglag_l20 --calibration-file $cal
```

Each successful command saves a new `attempt_NNN`. If the frame count, transfer,
per-frame XZERO, timestamp order, minimum trigger spacing, target coverage,
sample rate, interpolation ratio, clipping check, or SCPI event queue fails, the
attempt is marked invalid and must not be repaired by renumbering frames.

## Output layout

Each formal attempt contains at least:

```text
CAPTURE_COMPLETE.json or CAPTURE_INVALID.json
contract_snapshot.json
requested_scope_config.json
effective_scope_config.json
preamble_common.json
fastframe_timestamps.json
raw_frames.npy
raw/frame_0001_ch3.npy ... raw/frame_0120_ch3.npy
raw/frame_0001_meta.json ... raw/frame_0120_meta.json
frame_index.csv
point_summary.csv
capture_manifest.json
qc_report.json
scpi_transcript.json
derived/<point_id>.npz
plots/point_mean_overlays.png
```

For timingLag scans, the first derived axis is the untouched AUX-trigger-relative
time. The second subtracts only the commanded timingLag offset. For gateLen, the
second axis uses the programmed tfall center plus the one fixed path calibration.

## Important operational limitations

- Never press Autoset, Clear, Run/Stop, or change horizontal/vertical settings
  after the program prints `SCOPE READY`.
- The AWG program reports that TRIG_pg has no public stop/DONE method. If the AWG
  side fails after starting, its finite global trigger train may continue until
  its configured count naturally ends. Treat the scope attempt as invalid.
- The minimum possible interval between legal marker edges is based on the full
  AWG SRAM period: `32768 / 2 GHz = 16.384 us`. A shorter FastFrame timestamp
  interval is flagged as an extra/noise trigger.
- Keep the same AUX and CH3 cables after path calibration. Reconnect or reroute
  either cable and the preview must be repeated.
- Tek preamble converts CH3 ADC codes to the actual voltage seen at the scope
  input. It does not calibrate AWG DAC code to the positive output inside the
  fridge.
