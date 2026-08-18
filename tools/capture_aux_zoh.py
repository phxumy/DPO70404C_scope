import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyvisa

import scope_aux_center as scope_tools


RESOURCE = scope_tools.RESOURCE
TARGET_DELAY = 7.317975588e-6
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "adc_calibration"


def configure(scope):
    scope_tools.configure_aux(scope)
    scope.write("ACQUIRE:STATE STOP")
    scope.write("FASTACQ:STATE OFF")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")
    scope.write("HORIZONTAL:DELAY:MODE ON")
    scope.write(f"HORIZONTAL:DELAY:TIME {TARGET_DELAY:.12e}")
    scope.write("HORIZONTAL:MODE:SCALE 4E-9")
    scope.write("CH3:SCALE 10E-3")
    scope.write("CH3:POSITION 0")
    scope.write("CH3:OFFSET 0.2432")


def capture(scope, shots):
    times = []
    voltages = []
    raw_codes = []
    xincrs = []

    for shot in range(shots):
        if not scope_tools.arm_single_and_wait(scope, timeout_s=8.0):
            raise RuntimeError(f"No AUX trigger for shot {shot + 1}")
        time_axis, voltage, raw, xincr = scope_tools.read_ch3(scope)
        if raw.min() <= -127 or raw.max() >= 126:
            raise RuntimeError(
                f"ADC clipping in shot {shot + 1}: {raw.min()}..{raw.max()}"
            )
        times.append(time_axis)
        voltages.append(voltage)
        raw_codes.append(raw)
        xincrs.append(xincr)
        print(
            f"shot {shot + 1:02d}/{shots}: "
            f"xzero-grid start={time_axis[0] * 1e6:.9f} us, "
            f"raw={raw.min()}..{raw.max()}"
        )

    return (
        np.stack(times),
        np.stack(voltages),
        np.stack(raw_codes),
        np.asarray(xincrs),
    )


def common_grid_average(times, voltages, xincr):
    start = float(np.max(times[:, 0]))
    stop = float(np.min(times[:, -1]))
    count = int(np.floor((stop - start) / xincr)) + 1
    common_time = start + xincr * np.arange(count)
    aligned = np.stack(
        [np.interp(common_time, t, v) for t, v in zip(times, voltages)]
    )
    return common_time, aligned, np.mean(aligned, axis=0), np.std(aligned, axis=0)


def consecutive_runs(values):
    if len(values) == 0:
        return np.array([], dtype=int)
    change = np.r_[True, values[1:] != values[:-1], True]
    boundaries = np.flatnonzero(change)
    return np.diff(boundaries)


def analyze_and_plot(times, voltages, raw_codes, xincrs):
    xincr = float(np.median(xincrs))
    common_time, aligned, mean_voltage, std_voltage = common_grid_average(
        times, voltages, xincr
    )

    # Smooth only for locating the steepest physical edge. The displayed
    # single-shot points below remain completely untouched.
    smooth_kernel = np.ones(7) / 7
    smooth = np.convolve(mean_voltage, smooth_kernel, mode="same")
    guard = 20
    derivative = np.gradient(smooth, common_time)
    edge_index = guard + int(
        np.argmax(np.abs(derivative[guard:-guard]))
    )
    edge_time = float(common_time[edge_index])

    half_width = 1.4e-9
    single_mask = np.abs(times[0] - edge_time) <= half_width
    mean_mask = np.abs(common_time - edge_time) <= half_width

    single_time = times[0, single_mask]
    single_voltage = voltages[0, single_mask]
    single_raw = raw_codes[0, single_mask]
    adjacent_steps = np.diff(single_raw)
    exact_runs = consecutive_runs(single_raw)

    # Look only at the central, steep part of the selected edge when
    # reporting plateau runs. Flat extrema are not evidence of ZOH.
    local_v = single_voltage
    lo = np.min(local_v) + 0.2 * np.ptp(local_v)
    hi = np.min(local_v) + 0.8 * np.ptp(local_v)
    steep_mask = (local_v >= lo) & (local_v <= hi)
    steep_raw = single_raw[steep_mask]
    steep_runs = consecutive_runs(steep_raw)

    fig, axes = plt.subplots(3, 1, figsize=(12, 11), constrained_layout=True)

    axes[0].plot(times[0] * 1e6, voltages[0] * 1e3, ".-", ms=2, lw=0.6,
                 label="single shot: real 40 ps samples")
    axes[0].plot(common_time * 1e6, mean_voltage * 1e3, lw=1.2,
                 label=f"{len(times)}-shot trigger-aligned mean")
    axes[0].axvline(edge_time * 1e6, color="red", ls="--", lw=1)
    axes[0].set_ylabel("CH3 (mV)")
    axes[0].set_title("AUX-triggered CH3 acquisition")
    axes[0].grid(True)
    axes[0].legend()

    axes[1].plot(
        (single_time - edge_time) * 1e9,
        single_voltage * 1e3,
        "o-",
        ms=4,
        lw=0.8,
        label="unaltered single-shot samples",
    )
    axes[1].set_ylabel("CH3 (mV)")
    axes[1].set_title("Raw-point zoom around the steepest edge")
    axes[1].grid(True)
    axes[1].legend()

    axes[2].errorbar(
        (common_time[mean_mask] - edge_time) * 1e9,
        mean_voltage[mean_mask] * 1e3,
        yerr=std_voltage[mean_mask] * 1e3,
        fmt="o-",
        ms=3,
        lw=0.8,
        elinewidth=0.5,
        capsize=1,
        label="mean ± shot-to-shot standard deviation",
    )
    axes[2].set_xlabel("Time from selected edge center (ns)")
    axes[2].set_ylabel("CH3 (mV)")
    axes[2].set_title("Trigger-aligned average; markers remain 40 ps apart")
    axes[2].grid(True)
    axes[2].legend()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_DIR / "CH3_aux_zoh_zoom.png", dpi=180)
    plt.close(fig)

    ultra_half_width = 0.5e-9
    ultra_mask = np.abs(times[0] - edge_time) <= ultra_half_width
    ultra_time = times[0, ultra_mask]
    ultra_voltage = voltages[0, ultra_mask]
    ultra_raw = raw_codes[0, ultra_mask]
    ultra_x = (ultra_time - edge_time) * 1e12

    ultra_fig, ultra_axes = plt.subplots(
        2, 1, figsize=(11, 8), constrained_layout=True
    )
    ultra_axes[0].scatter(ultra_x, ultra_voltage * 1e3, s=45)
    ultra_axes[0].set_ylabel("CH3 (mV)")
    ultra_axes[0].set_title(
        "1 ns ultra-zoom: actual single-shot samples only (no connecting line)"
    )
    ultra_axes[0].grid(True)

    ultra_axes[1].scatter(ultra_x, ultra_raw, s=45)
    ultra_axes[1].set_xlabel("Time from selected edge center (ps)")
    ultra_axes[1].set_ylabel("Raw ADC code")
    ultra_axes[1].set_title("Raw ADC codes; neighboring markers are 40 ps apart")
    ultra_axes[1].grid(True)
    ultra_fig.savefig(OUTPUT_DIR / "CH3_aux_zoh_ultrazoom.png", dpi=200)
    plt.close(ultra_fig)

    min_times = times[np.arange(len(times)), np.argmin(voltages, axis=1)]
    max_times = times[np.arange(len(times)), np.argmax(voltages, axis=1)]

    print("\nZOH-oriented diagnostics")
    print(f"Real sample spacing             : {xincr * 1e12:.3f} ps")
    print(f"Selected edge time after AUX    : {edge_time * 1e6:.9f} us")
    print(f"Points in 2.8 ns zoom           : {len(single_raw)}")
    print(f"Largest equal-code run, zoom    : {exact_runs.max(initial=0)} points")
    print(f"Largest equal-code run, slope   : {steep_runs.max(initial=0)} points")
    print(f"Median |adjacent code change|   : {np.median(np.abs(adjacent_steps)):.3f} codes")
    print(f"Minimum-time jitter across shots: {np.std(min_times) * 1e12:.3f} ps")
    print(f"Maximum-time jitter across shots: {np.std(max_times) * 1e12:.3f} ps")
    print("Saved plot                     : CH3_aux_zoh_zoom.png")
    print("Saved ultra-zoom               : CH3_aux_zoh_ultrazoom.png")

    return common_time, aligned, mean_voltage, std_voltage, edge_time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=int, default=24)
    args = parser.parse_args()

    rm = pyvisa.ResourceManager()
    scope = rm.open_resource(RESOURCE)
    scope.timeout = 10000
    try:
        print(scope.query("*IDN?").strip())
        configure(scope)
        times, voltages, raw_codes, xincrs = capture(scope, args.shots)
        common_time, aligned, mean_voltage, std_voltage, edge_time = (
            analyze_and_plot(times, voltages, raw_codes, xincrs)
        )
        np.savez(
            OUTPUT_DIR / "CH3_aux_zoh_shots.npz",
            times=times,
            voltages=voltages,
            raw_codes=raw_codes,
            xincrs=xincrs,
            common_time=common_time,
            aligned_voltages=aligned,
            mean_voltage=mean_voltage,
            std_voltage=std_voltage,
            selected_edge_time=edge_time,
            trigger_source="AUXILIARY",
            aux_level=1.4,
            horizontal_delay=TARGET_DELAY,
            vertical_scale=10e-3,
            vertical_offset=0.2432,
        )
        zoom_position = 100.0 * (
            edge_time - times[-1, 0]
        ) / (times[-1, -1] - times[-1, 0])
        scope.write("ZOOM:ZOOM1:STATE ON")
        scope.write("ZOOM:ZOOM1:CH3:DISPLAY ON")
        scope.write(f"ZOOM:HORIZONTAL:POSITION {zoom_position:.6f}")
        scope.write("ZOOM:HORIZONTAL:SCALE 20")
        scope.write("ZOOM:GRATICULE:SIZE 80")
        scope.write("ZOOM:STATE ON")
        print(
            f"Scope MultiView Zoom           : 20x at {zoom_position:.3f}%"
        )
        print("Saved data                     : CH3_aux_zoh_shots.npz")
    finally:
        scope.close()
        rm.close()


if __name__ == "__main__":
    main()
