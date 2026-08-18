import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyvisa

import scope_aux_center as scope_tools


RESOURCE = scope_tools.RESOURCE
AWG_SAMPLE_RATE = 2.0e9
MARKER_TO_STAIR = 100.0e-9
DEFAULT_HOLD_SAMPLES = 8
LEVEL_COUNT = 8
COARSE_SCOPE_SCALE = 8.0e-9
FINE_SCOPE_SCALE = 4.0e-9
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "adc_calibration"


def configure(scope, delay, horizontal_scale, vertical_scale, vertical_offset):
    scope_tools.configure_aux(scope)
    scope.write("ACQUIRE:STATE STOP")
    scope.write("FASTACQ:STATE OFF")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")
    scope.write("HORIZONTAL:DELAY:MODE ON")
    scope.write(f"HORIZONTAL:DELAY:TIME {delay:.12e}")
    scope.write(f"HORIZONTAL:MODE:SCALE {horizontal_scale:.12e}")
    scope.write("HORIZONTAL:POSITION 0")
    scope.write(f"CH3:SCALE {vertical_scale:.12e}")
    scope.write("CH3:POSITION 0")
    scope.write(f"CH3:OFFSET {vertical_offset:.12e}")
    scope.write("ZOOM:STATE OFF")


def acquire(scope, shots):
    times = []
    voltages = []
    raw_codes = []
    xincrs = []
    for shot in range(shots):
        if not scope_tools.arm_single_and_wait(scope, timeout_s=8.0):
            raise RuntimeError(f"No AUX trigger for shot {shot + 1}")
        time_axis, voltage, raw, xincr = scope_tools.read_ch3(scope)
        times.append(time_axis)
        voltages.append(voltage)
        raw_codes.append(raw)
        xincrs.append(xincr)
        print(
            f"shot {shot + 1:02d}/{shots}: "
            f"record={time_axis[0] * 1e9:.3f}..{time_axis[-1] * 1e9:.3f} ns, "
            f"voltage={voltage.min() * 1e3:.3f}..{voltage.max() * 1e3:.3f} mV, "
            f"raw={raw.min()}..{raw.max()}"
        )
    return (
        np.stack(times),
        np.stack(voltages),
        np.stack(raw_codes),
        np.asarray(xincrs),
    )


def align_to_common_grid(times, voltages, xincr):
    start = float(np.max(times[:, 0]))
    stop = float(np.min(times[:, -1]))
    count = int(np.floor((stop - start) / xincr)) + 1
    common_time = start + xincr * np.arange(count)
    aligned = np.stack(
        [np.interp(common_time, t, v) for t, v in zip(times, voltages)]
    )
    return common_time, aligned


def choose_vertical_settings(time_axis, voltage):
    pre_mask = time_axis < MARKER_TO_STAIR - 2.0e-9
    if np.any(pre_mask):
        baseline = float(np.median(voltage[pre_mask]))
    else:
        baseline = float(np.median(voltage))

    max_deviation = float(np.max(np.abs(voltage - baseline)))
    required_scale = max_deviation / 3.8
    scales = np.array([2, 5, 10, 20, 50, 100], dtype=float) * 1e-3
    valid = scales[scales >= required_scale]
    vertical_scale = float(valid[0] if len(valid) else scales[-1])
    return baseline, vertical_scale


def estimate_path_delay(time_axis, voltage, expected_boundaries):
    kernel = np.ones(7, dtype=float) / 7.0
    smooth = np.convolve(voltage, kernel, mode="same")
    derivative = np.abs(np.gradient(smooth, time_axis))
    sample_spacing = float(np.median(np.diff(time_axis)))
    candidates = np.arange(-20e-9, 20e-9 + sample_spacing, sample_spacing)
    scores = []
    for shift in candidates:
        sample_times = expected_boundaries + shift
        values = np.interp(sample_times, time_axis, derivative, left=0, right=0)
        scores.append(float(np.sum(np.sqrt(values))))
    return float(candidates[int(np.argmax(scores))])


def locate_transitions(
    common_time, mean_voltage, physical_boundaries, hold_time
):
    kernel = np.ones(5, dtype=float) / 5.0
    smooth = np.convolve(mean_voltage, kernel, mode="same")
    derivative = np.gradient(smooth, common_time)
    actual = []
    search_half_width = min(1.5e-9, 0.45 * hold_time)
    for boundary in physical_boundaries:
        mask = np.abs(common_time - boundary) <= search_half_width
        if not np.any(mask):
            actual.append(np.nan)
            continue
        indices = np.flatnonzero(mask)
        best = indices[int(np.argmax(np.abs(derivative[mask])))]
        actual.append(float(common_time[best]))
    return np.asarray(actual)


def analyze_and_plot(
    times, voltages, raw_codes, xincrs, path_delay, hold_samples
):
    hold_time = hold_samples / AWG_SAMPLE_RATE
    stair_end = MARKER_TO_STAIR + LEVEL_COUNT * hold_time
    expected_boundaries = MARKER_TO_STAIR + hold_time * np.arange(
        LEVEL_COUNT + 1
    )
    physical_boundaries = expected_boundaries + path_delay
    physical_start = MARKER_TO_STAIR + path_delay
    physical_end = stair_end + path_delay

    xincr = float(np.median(xincrs))
    common_time, aligned = align_to_common_grid(times, voltages, xincr)
    mean_voltage = np.mean(aligned, axis=0)
    std_voltage = np.std(aligned, axis=0)
    transitions = locate_transitions(
        common_time, mean_voltage, physical_boundaries, hold_time
    )

    plateau_means = []
    plateau_stds = []
    plateau_slopes = []
    for level in range(LEVEL_COUNT):
        # Measure the middle 60% of each programmed hold interval.
        left = physical_start + (level + 0.30) * hold_time
        right = physical_start + (level + 0.90) * hold_time
        mask = (common_time >= left) & (common_time <= right)
        values = mean_voltage[mask]
        plateau_means.append(float(np.mean(values)))
        plateau_stds.append(float(np.std(values)))
        if len(values) >= 2:
            coefficient = np.polyfit(common_time[mask], values, 1)[0]
            plateau_slopes.append(float(coefficient))
        else:
            plateau_slopes.append(np.nan)

    plateau_means = np.asarray(plateau_means)
    plateau_stds = np.asarray(plateau_stds)
    plateau_slopes = np.asarray(plateau_slopes)

    fig, axes = plt.subplots(3, 1, figsize=(13, 11), constrained_layout=True)
    axes[0].plot(
        times[0] * 1e9,
        voltages[0] * 1e3,
        ".",
        ms=2,
        label=f"single shot: actual {xincr * 1e12:.0f} ps samples",
    )
    axes[0].plot(
        common_time * 1e9,
        mean_voltage * 1e3,
        lw=1.2,
        label=f"{len(times)}-shot AUX-aligned mean",
    )
    for boundary in physical_boundaries:
        axes[0].axvline(boundary * 1e9, color="green", ls="--", lw=0.7)
    axes[0].set_ylabel("CH3 (mV)")
    axes[0].set_title(
        "Appended staircase after AUX marker; green lines include fitted path delay"
    )
    axes[0].grid(True)
    axes[0].legend()

    stair_mask = (
        (times[0] >= physical_start - 1.0e-9)
        & (times[0] <= physical_end + 1.0e-9)
    )
    axes[1].scatter(
        times[0, stair_mask] * 1e9,
        voltages[0, stair_mask] * 1e3,
        s=7,
        label="unaltered single-shot samples",
    )
    for boundary in physical_boundaries:
        axes[1].axvline(boundary * 1e9, color="green", ls="--", lw=0.7)
    for boundary in transitions:
        if np.isfinite(boundary):
            axes[1].axvline(boundary * 1e9, color="red", ls=":", lw=0.8)
    axes[1].set_ylabel("CH3 (mV)")
    axes[1].set_title(
        "Actual samples only; red dotted lines are detected analog transitions"
    )
    axes[1].grid(True)
    axes[1].legend()

    levels = np.arange(1, LEVEL_COUNT + 1)
    axes[2].errorbar(
        levels,
        plateau_means * 1e3,
        yerr=plateau_stds * 1e3,
        fmt="o-",
        capsize=3,
        label="settled part of each programmed level",
    )
    axes[2].set_xlabel("Programmed staircase level")
    axes[2].set_ylabel("CH3 mean (mV)")
    axes[2].set_xticks(levels)
    axes[2].set_title("Measured plateau levels")
    axes[2].grid(True)
    axes[2].legend()

    output_stem = f"CH3_appended_staircase_hold{hold_samples}"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_DIR / f"{output_stem}.png", dpi=190)
    plt.close(fig)

    np.savez(
        OUTPUT_DIR / f"{output_stem}.npz",
        times=times,
        voltages=voltages,
        raw_codes=raw_codes,
        xincrs=xincrs,
        common_time=common_time,
        aligned_voltages=aligned,
        mean_voltage=mean_voltage,
        std_voltage=std_voltage,
        expected_boundaries=expected_boundaries,
        physical_boundaries=physical_boundaries,
        detected_transitions=transitions,
        plateau_means=plateau_means,
        plateau_stds=plateau_stds,
        plateau_slopes=plateau_slopes,
        marker_to_stair=MARKER_TO_STAIR,
        fitted_path_delay=path_delay,
        awg_sample_rate=AWG_SAMPLE_RATE,
        hold_samples=hold_samples,
    )

    print("\nAppended staircase diagnostics")
    print(f"Scope sample spacing      : {xincr * 1e12:.3f} ps")
    print(f"Expected staircase start : {MARKER_TO_STAIR * 1e9:.3f} ns")
    print(f"Fitted output-path delay : {path_delay * 1e9:.3f} ns")
    print(f"Physical staircase start : {physical_start * 1e9:.3f} ns")
    print(f"Expected hold time        : {hold_time * 1e9:.3f} ns")
    print(f"Expected staircase end   : {stair_end * 1e9:.3f} ns")
    print(f"Measured peak-to-peak     : {np.ptp(mean_voltage) * 1e3:.3f} mV")
    print("Detected transitions     : " + ", ".join(
        "nan" if not np.isfinite(t) else f"{t * 1e9:.3f} ns"
        for t in transitions
    ))
    print("Plateau means            : " + ", ".join(
        f"{value * 1e3:.3f} mV" for value in plateau_means
    ))
    print("Within-plateau std       : " + ", ".join(
        f"{value * 1e3:.3f} mV" for value in plateau_stds
    ))
    print("Plateau slopes           : " + ", ".join(
        f"{value * 1e-6:.3f} mV/ns" for value in plateau_slopes
    ))
    print(f"Saved                    : {output_stem}.png")
    print(f"Saved                    : {output_stem}.npz")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=int, default=12)
    parser.add_argument(
        "--hold-samples", type=int, default=DEFAULT_HOLD_SAMPLES
    )
    args = parser.parse_args()

    if args.hold_samples < 1:
        parser.error("--hold-samples must be a positive integer")
    hold_samples = args.hold_samples
    hold_time = hold_samples / AWG_SAMPLE_RATE
    nominal_delay = MARKER_TO_STAIR + 0.5 * LEVEL_COUNT * hold_time
    expected_boundaries = MARKER_TO_STAIR + hold_time * np.arange(
        LEVEL_COUNT + 1
    )

    rm = pyvisa.ResourceManager()
    scope = rm.open_resource(RESOURCE)
    scope.timeout = 10000
    try:
        print(scope.query("*IDN?").strip())

        # First use generous headroom to learn the new waveform's baseline
        # and amplitude without risking ADC clipping.
        configure(scope, nominal_delay, COARSE_SCOPE_SCALE, 20e-3, 0.2432)
        if not scope_tools.arm_single_and_wait(scope, timeout_s=8.0):
            raise RuntimeError("No AUX trigger during preliminary acquisition")
        preview_t, preview_v, preview_raw, _ = scope_tools.read_ch3(scope)
        baseline, vertical_scale = choose_vertical_settings(preview_t, preview_v)
        path_delay = estimate_path_delay(
            preview_t, preview_v, expected_boundaries
        )
        target_delay = nominal_delay + path_delay
        if preview_raw.min() <= -127 or preview_raw.max() >= 126:
            vertical_scale = max(vertical_scale, 50e-3)

        print("\nPreliminary acquisition")
        print(f"Baseline                 : {baseline * 1e3:.3f} mV")
        print(f"Peak-to-peak             : {np.ptp(preview_v) * 1e3:.3f} mV")
        print(f"Selected vertical scale  : {vertical_scale * 1e3:.3f} mV/div")
        print(f"Fitted output-path delay : {path_delay * 1e9:.3f} ns")
        print(f"Fine-record center       : {target_delay * 1e9:.3f} ns")

        configure(
            scope,
            target_delay,
            FINE_SCOPE_SCALE,
            vertical_scale,
            baseline,
        )
        times, voltages, raw_codes, xincrs = acquire(scope, args.shots)
        analyze_and_plot(
            times,
            voltages,
            raw_codes,
            xincrs,
            path_delay,
            hold_samples,
        )

        # Leave the most recent single acquisition frozen and centered.
        scope.write("ACQUIRE:STATE STOP")
        scope.write("ZOOM:STATE OFF")
        print(f"Scope frozen at AUX + {target_delay * 1e9:.3f} ns")
    finally:
        scope.close()
        rm.close()


if __name__ == "__main__":
    main()
