import argparse
import time
from pathlib import Path

import numpy as np
import pyvisa


RESOURCE = "TCPIP0::192.168.1.7::inst0::INSTR"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "adc_calibration"


def query(scope, command):
    try:
        return scope.query(command).strip()
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def audit(scope, title="CURRENT SCOPE STATE"):
    commands = [
        ("FastAcq", "FASTACQ:STATE?"),
        ("Acquisition state", "ACQUIRE:STATE?"),
        ("Sampling mode", "ACQUIRE:SAMPLINGMODE?"),
        ("Acquisition mode", "ACQUIRE:MODE?"),
        ("Actual acquisition mode", "ACQUIRE:MODE:ACTUAL?"),
        ("Interpolation ratio", "HORIZONTAL:MAIN:INTERPRATIO?"),
        ("Horizontal mode", "HORIZONTAL:MODE?"),
        ("Horizontal scale", "HORIZONTAL:MODE:SCALE?"),
        ("Sample rate", "HORIZONTAL:MODE:SAMPLERATE?"),
        ("Record length", "HORIZONTAL:MODE:RECORDLENGTH?"),
        ("Horizontal delay mode", "HORIZONTAL:DELAY:MODE?"),
        ("Horizontal delay time", "HORIZONTAL:DELAY:TIME?"),
        ("Horizontal position", "HORIZONTAL:POSITION?"),
        ("CH1 displayed", "SELECT:CH1?"),
        ("CH2 displayed", "SELECT:CH2?"),
        ("CH3 displayed", "SELECT:CH3?"),
        ("CH4 displayed", "SELECT:CH4?"),
        ("CH3 termination", "CH3:TERMINATION?"),
        ("CH3 coupling", "CH3:COUPLING?"),
        ("CH3 bandwidth", "CH3:BANDWIDTH?"),
        ("CH3 scale", "CH3:SCALE?"),
        ("CH3 position", "CH3:POSITION?"),
        ("CH3 offset", "CH3:OFFSET?"),
        ("Trigger type", "TRIGGER:A:TYPE?"),
        ("Trigger source", "TRIGGER:A:EDGE:SOURCE?"),
        ("AUX slope", "TRIGGER:A:EDGE:SLOPE:AUX?"),
        ("AUX level", "TRIGGER:AUXLEVEL?"),
        ("CH3 trigger level", "TRIGGER:A:LEVEL:CH3?"),
        ("Trigger mode", "TRIGGER:A:MODE?"),
        ("Trigger state", "TRIGGER:STATE?"),
    ]

    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    for label, command in commands:
        print(f"{label:28s}: {query(scope, command)}")


def configure_aux(scope):
    # Restore the acquisition and CH3 settings that Autoset may have changed.
    scope.write("ACQUIRE:STATE STOP")
    scope.write("FASTACQ:STATE OFF")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")

    scope.write("SELECT:CH3 ON")
    scope.write("CH3:TERMINATION 50")
    scope.write("CH3:COUPLING DC")
    scope.write("CH3:BANDWIDTH FULL")

    scope.write("TRIGGER:A:TYPE EDGE")
    scope.write("TRIGGER:A:EDGE:SOURCE AUXILIARY")
    scope.write("TRIGGER:A:EDGE:SLOPE:AUX RISE")
    scope.write("TRIGGER:AUXLEVEL TTL")
    scope.write("TRIGGER:A:MODE NORMAL")


def arm_single_and_wait(scope, timeout_s=8.0):
    scope.write("ACQUIRE:STOPAFTER SEQUENCE")
    scope.write("ACQUIRE:STATE ON")

    deadline = time.monotonic() + timeout_s
    states = []
    while time.monotonic() < deadline:
        state = query(scope, "TRIGGER:STATE?")
        if not states or state != states[-1]:
            states.append(state)
            print(f"Trigger state: {state}")

        acquiring = query(scope, "ACQUIRE:STATE?")
        if acquiring in {"0", "OFF", "STOP"}:
            return True
        time.sleep(0.1)
    return False


def try_one_trigger(scope, level, slope, timeout_s=1.25):
    scope.write("ACQUIRE:STATE STOP")
    scope.write(f"TRIGGER:A:EDGE:SLOPE:AUX {slope}")
    scope.write(f"TRIGGER:AUXLEVEL {level:.6g}")
    scope.write("TRIGGER:A:MODE NORMAL")
    scope.write("ACQUIRE:STOPAFTER SEQUENCE")
    scope.write("ACQUIRE:STATE ON")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        acquiring = query(scope, "ACQUIRE:STATE?")
        if acquiring in {"0", "OFF", "STOP"}:
            return True
        time.sleep(0.05)
    scope.write("ACQUIRE:STATE STOP")
    return False


def sweep_aux_trigger(scope):
    configure_aux(scope)
    levels = (
        -2.50,
        -1.80,
        -1.40,
        -1.00,
        -0.50,
        -0.20,
        -0.05,
        0.05,
        0.20,
        0.50,
        1.00,
        1.40,
        1.80,
        2.50,
    )
    successes = []
    print("\nScanning AUX trigger threshold")
    for slope in ("RISE", "FALL"):
        for level in levels:
            ok = try_one_trigger(scope, level, slope)
            print(f"{slope:4s}  level={level:5.2f} V  triggered={ok}")
            if ok:
                successes.append((slope, level))

    if successes:
        # Prefer a threshold away from zero and in the middle of a working
        # range, which is less vulnerable to noise than the first hit.
        best_slope = max(
            {s for s, _ in successes},
            key=lambda s: sum(1 for hit_slope, _ in successes if hit_slope == s),
        )
        working_levels = [level for slope, level in successes if slope == best_slope]
        best_level = working_levels[len(working_levels) // 2]
        scope.write(f"TRIGGER:A:EDGE:SLOPE:AUX {best_slope}")
        scope.write(f"TRIGGER:AUXLEVEL {best_level:.6g}")
        scope.write("TRIGGER:A:MODE NORMAL")
        print(
            f"\nSelected AUX trigger: slope={best_slope}, "
            f"level={best_level:.3f} V"
        )
    else:
        scope.write("TRIGGER:A:EDGE:SLOPE:AUX RISE")
        scope.write("TRIGGER:AUXLEVEL TTL")
        print("\nNo AUX triggers detected at any tested threshold or slope.")
    return successes


def read_ch3(scope):
    scope.write("DATA:SOURCE CH3")
    record_length = int(float(scope.query("HORIZONTAL:MODE:RECORDLENGTH?")))
    scope.write("DATA:START 1")
    scope.write(f"DATA:STOP {record_length}")
    scope.write("DATA:ENCDG SRIbinary")
    scope.write("WFMOUTPRE:BYT_NR 1")

    byte_nr = int(float(scope.query("WFMOUTPRE:BYT_NR?")))
    if byte_nr == 1:
        datatype = "b"
    elif byte_nr == 2:
        datatype = "h"
    else:
        raise RuntimeError(f"Unsupported BYT_NR={byte_nr}")

    raw = scope.query_binary_values(
        "CURVE?",
        datatype=datatype,
        is_big_endian=False,
        container=np.array,
    )

    xincr = float(scope.query("WFMOUTPRE:XINCR?"))
    xzero = float(scope.query("WFMOUTPRE:XZERO?"))
    pt_off = float(scope.query("WFMOUTPRE:PT_OFF?"))
    ymult = float(scope.query("WFMOUTPRE:YMULT?"))
    yoff = float(scope.query("WFMOUTPRE:YOFF?"))
    yzero = float(scope.query("WFMOUTPRE:YZERO?"))

    index = np.arange(len(raw), dtype=float)
    time_axis = xzero + xincr * (index - pt_off)
    voltage = yzero + ymult * (raw.astype(float) - yoff)
    return time_axis, voltage, raw, xincr


def find_feature_center(time_axis, voltage, final_span=40e-9):
    # Remove the robust baseline and search for the 40 ns window with the most
    # waveform energy. This centers a bipolar pulse more reliably than using
    # only its largest positive or negative sample.
    baseline = float(np.median(voltage))
    centered = voltage - baseline
    dt = float(np.median(np.diff(time_axis)))
    window_points = max(1, min(len(voltage), int(round(final_span / dt))))
    kernel = np.ones(window_points, dtype=float)
    energy = np.convolve(centered * centered, kernel, mode="same")
    peak_index = int(np.argmax(energy))
    half_window = max(1, window_points // 2)
    start = max(0, peak_index - half_window)
    stop = min(len(voltage), peak_index + half_window + 1)
    local_weights = centered[start:stop] ** 2
    if float(np.sum(local_weights)) > 0:
        feature_time = float(
            np.sum(time_axis[start:stop] * local_weights) / np.sum(local_weights)
        )
    else:
        feature_time = float(time_axis[peak_index])
    return feature_time, baseline, window_points


def configure_ch3_fallback(scope, level=0.2226):
    scope.write("ACQUIRE:STATE STOP")
    scope.write("FASTACQ:STATE OFF")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")

    scope.write("SELECT:CH3 ON")
    scope.write("CH3:TERMINATION 50")
    scope.write("CH3:COUPLING DC")
    scope.write("CH3:BANDWIDTH FULL")
    scope.write("CH3:SCALE 40E-3")
    scope.write("CH3:OFFSET 0")
    scope.write("CH3:POSITION -5")

    scope.write("TRIGGER:A:TYPE EDGE")
    scope.write("TRIGGER:A:EDGE:SOURCE CH3")
    scope.write("TRIGGER:A:EDGE:SLOPE:CH3 RISE")
    scope.write(f"TRIGGER:A:LEVEL:CH3 {level:.6g}")
    scope.write("TRIGGER:A:EDGE:SLOPE:AUX RISE")
    scope.write("TRIGGER:AUXLEVEL TTL")
    scope.write("TRIGGER:A:MODE NORMAL")


def center_with_ch3_fallback(scope):
    configure_ch3_fallback(scope)

    # Coarse search: keep the full 10 us post-trigger record created by
    # Autoset. At 125 MS/s this locates the target to within a few ns.
    scope.write("HORIZONTAL:DELAY:MODE OFF")
    scope.write("HORIZONTAL:POSITION 0")
    scope.write("HORIZONTAL:MODE:SCALE 1E-6")
    scope.write("ACQUIRE:SAMPLINGMODE RT")

    print("\nCoarse CH3-triggered acquisition")
    if not arm_single_and_wait(scope, timeout_s=8.0):
        raise RuntimeError(
            "CH3 did not trigger at 222.6 mV. "
            "Check that the AWG waveform is repeating."
        )

    coarse_t, coarse_v, _, coarse_dt = read_ch3(scope)
    coarse_center, _, _ = find_feature_center(coarse_t, coarse_v, final_span=80e-9)
    print(f"Coarse dt                 : {coarse_dt * 1e9:.3f} ns")
    print(f"Coarse feature center     : {coarse_center * 1e6:.9f} us")
    print(f"Coarse peak-to-peak       : {np.ptp(coarse_v) * 1e3:.3f} mV")

    # Fine search: acquire a real-time 40 ns window around the coarse result.
    scope.write("ACQUIRE:STATE STOP")
    scope.write("HORIZONTAL:DELAY:MODE ON")
    scope.write(f"HORIZONTAL:DELAY:TIME {coarse_center:.12e}")
    scope.write("HORIZONTAL:MODE:SCALE 4E-9")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")
    scope.write("CH3:SCALE 30E-3")
    scope.write("CH3:OFFSET 0")
    scope.write("CH3:POSITION -5")

    print("\nFine CH3-triggered acquisition")
    if not arm_single_and_wait(scope, timeout_s=8.0):
        raise RuntimeError("CH3 stopped triggering during the fine acquisition.")

    fine_t, fine_v, fine_raw, fine_dt = read_ch3(scope)
    fine_center, fine_baseline, _ = find_feature_center(
        fine_t, fine_v, final_span=12e-9
    )
    print(f"Fine dt                   : {fine_dt * 1e12:.3f} ps")
    print(f"Fine record start         : {fine_t[0] * 1e6:.9f} us")
    print(f"Fine record end           : {fine_t[-1] * 1e6:.9f} us")
    print(f"Fine feature center       : {fine_center * 1e6:.9f} us")
    print(f"Fine peak-to-peak         : {np.ptp(fine_v) * 1e3:.3f} mV")

    # Center both axes. A 20 mV/div scale gives 0.8 mV/code on this scope,
    # while the measured ~90 mV p-p signal still has generous headroom.
    scope.write("ACQUIRE:STATE STOP")
    scope.write(f"HORIZONTAL:DELAY:TIME {fine_center:.12e}")
    scope.write("CH3:SCALE 20E-3")
    scope.write("CH3:POSITION 0")
    scope.write(f"CH3:OFFSET {fine_baseline:.12e}")
    if not arm_single_and_wait(scope, timeout_s=8.0):
        raise RuntimeError("CH3 did not trigger for the final centered acquisition.")

    final_t, final_v, final_raw, final_dt = read_ch3(scope)
    if final_raw.min() <= -127 or final_raw.max() >= 126:
        print("20 mV/div clipped; falling back to 30 mV/div.")
        scope.write("ACQUIRE:STATE STOP")
        scope.write("CH3:SCALE 30E-3")
        if not arm_single_and_wait(scope, timeout_s=8.0):
            raise RuntimeError("No trigger after vertical-scale fallback.")
        final_t, final_v, final_raw, final_dt = read_ch3(scope)

    final_scale = float(scope.query("CH3:SCALE?"))
    final_offset = float(scope.query("CH3:OFFSET?"))
    final_ymult = float(scope.query("WFMOUTPRE:YMULT?"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(
        OUTPUT_DIR / "CH3_centered_waveform.npz",
        raw=final_raw,
        voltage=final_v,
        time=final_t,
        xincr=final_dt,
        horizontal_delay=fine_center,
        trigger_source="CH3",
        trigger_level=0.2226,
        vertical_scale=final_scale,
        vertical_offset=final_offset,
        ymult=final_ymult,
    )
    print("\nFinal centered acquisition completed and frozen.")
    print(f"Saved                     : {OUTPUT_DIR / 'CH3_centered_waveform.npz'}")
    print(f"Final peak-to-peak        : {np.ptp(final_v) * 1e3:.3f} mV")
    print(f"Final vertical scale      : {final_scale * 1e3:.3f} mV/div")
    print(f"Final vertical offset     : {final_offset * 1e3:.3f} mV")
    print(f"Final voltage/code        : {final_ymult * 1e3:.3f} mV")
    return fine_center


def center_current_record(scope):
    time_axis, voltage, raw, xincr = read_ch3(scope)
    target_time, baseline, window_points = find_feature_center(time_axis, voltage)

    print("\nWaveform diagnostic")
    print(f"Points                    : {len(raw)}")
    print(f"X increment               : {xincr * 1e12:.3f} ps")
    print(f"Record start              : {time_axis[0] * 1e6:.9f} us")
    print(f"Record end                : {time_axis[-1] * 1e6:.9f} us")
    print(f"Median baseline           : {baseline * 1e3:.3f} mV")
    print(f"Peak-to-peak              : {np.ptp(voltage) * 1e3:.3f} mV")
    print(f"Detection window          : {window_points} points")
    print(f"Detected feature center   : {target_time * 1e6:.9f} us")

    scope.write("ACQUIRE:STATE STOP")
    scope.write("HORIZONTAL:DELAY:MODE ON")
    scope.write(f"HORIZONTAL:DELAY:TIME {target_time:.12e}")
    scope.write("HORIZONTAL:MODE:SCALE 4E-9")
    scope.write("ACQUIRE:SAMPLINGMODE RT")
    scope.write("ACQUIRE:MODE SAMPLE")
    scope.write("ACQUIRE:STATE ON")
    return target_time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "audit",
            "configure",
            "test-trigger",
            "sweep-aux",
            "center",
            "center-ch3",
        ),
        nargs="?",
        default="audit",
    )
    args = parser.parse_args()

    rm = pyvisa.ResourceManager()
    scope = rm.open_resource(RESOURCE)
    scope.timeout = 10000

    try:
        print(scope.query("*IDN?").strip())
        if args.action == "audit":
            audit(scope)
        elif args.action == "configure":
            audit(scope, "BEFORE RESTORE")
            configure_aux(scope)
            audit(scope, "AFTER RESTORE")
        elif args.action == "test-trigger":
            configure_aux(scope)
            ok = arm_single_and_wait(scope)
            print(f"AUX single acquisition completed: {ok}")
            audit(scope, "AFTER AUX TRIGGER TEST")
        elif args.action == "sweep-aux":
            successes = sweep_aux_trigger(scope)
            print(f"Working AUX settings found: {len(successes)}")
            audit(scope, "AFTER AUX TRIGGER SWEEP")
        elif args.action == "center":
            target_time = center_current_record(scope)
            print(f"\nCentered at horizontal delay {target_time * 1e6:.9f} us")
            audit(scope, "AFTER CENTERING")
        elif args.action == "center-ch3":
            target_time = center_with_ch3_fallback(scope)
            print(f"\nCentered at horizontal delay {target_time * 1e6:.9f} us")
            audit(scope, "FINAL CH3 FALLBACK STATE")
    finally:
        scope.close()
        rm.close()


if __name__ == "__main__":
    main()
