import pyvisa
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path


RESOURCE = "TCPIP0::192.168.1.7::inst0::INSTR"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "adc_calibration"


# ============================================================
# 1. 连接示波器
# ============================================================

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)

scope.timeout = 10000


print("=" * 60)
print(scope.query("*IDN?").strip())
print("=" * 60)


# ============================================================
# 2. 指定 CH3，并保持当前 RT / Sample 设置
# ============================================================

scope.write("DATA:SOURCE CH3")

scope.write("FASTACQ:STATE OFF")
scope.write("ACQUIRE:SAMPLINGMODE RT")
scope.write("ACQUIRE:MODE SAMPLE")

scope.write("CH3:TERMINATION 50")
scope.write("CH3:COUPLING DC")
scope.write("CH3:BANDWIDTH FULL")


# ============================================================
# 3. 设置传输范围
# ============================================================

record_length = int(
    float(scope.query("HORIZONTAL:MODE:RECORDLENGTH?"))
)

scope.write("DATA:START 1")
scope.write(f"DATA:STOP {record_length}")


# ============================================================
# 4. 设置二进制 waveform 传输格式
#
# SRIbinary:
#   Signed integer
#   Little endian
#   适合 PC
# ============================================================

scope.write("DATA:ENCDG SRIbinary")

# 当前普通 Sample waveform 优先尝试 1 byte / point
scope.write("WFMOUTPRE:BYT_NR 1")


# ============================================================
# 5. 查询完整 waveform format
# ============================================================

byte_nr = int(float(scope.query("WFMOUTPRE:BYT_NR?")))
bit_nr = int(float(scope.query("WFMOUTPRE:BIT_NR?")))

bn_fmt = scope.query("WFMOUTPRE:BN_FMT?").strip()
byt_or = scope.query("WFMOUTPRE:BYT_OR?").strip()

nr_pt = int(float(scope.query("WFMOUTPRE:NR_PT?")))

xincr = float(scope.query("WFMOUTPRE:XINCR?"))
xzero = float(scope.query("WFMOUTPRE:XZERO?"))
pt_off = float(scope.query("WFMOUTPRE:PT_OFF?"))

ymult = float(scope.query("WFMOUTPRE:YMULT?"))
yoff = float(scope.query("WFMOUTPRE:YOFF?"))
yzero = float(scope.query("WFMOUTPRE:YZERO?"))


print("\nWaveform format:")
print(f"BYT_NR  = {byte_nr}")
print(f"BIT_NR  = {bit_nr}")
print(f"BN_FMT  = {bn_fmt}")
print(f"BYT_OR  = {byt_or}")
print(f"NR_PT   = {nr_pt}")

print("\nHorizontal:")
print(f"XINCR   = {xincr:.12e} s")
print(f"XZERO   = {xzero:.12e} s")
print(f"PT_OFF  = {pt_off}")

print("\nVertical:")
print(f"YMULT   = {ymult:.12e} V/code")
print(f"YOFF    = {yoff}")
print(f"YZERO   = {yzero:.12e} V")


# ============================================================
# 6. 读取 CURVE? 原始整数数据
# ============================================================

if byte_nr == 1:

    raw = scope.query_binary_values(
        "CURVE?",
        datatype="b",      # signed int8
        is_big_endian=False,
        container=np.array
    )

elif byte_nr == 2:

    raw = scope.query_binary_values(
        "CURVE?",
        datatype="h",      # signed int16
        is_big_endian=False,
        container=np.array
    )

else:
    raise RuntimeError(
        f"Unexpected BYT_NR = {byte_nr}"
    )


print()
print(f"Received {len(raw)} waveform points.")
print(f"raw min = {raw.min()}")
print(f"raw max = {raw.max()}")


# ============================================================
# 7. raw → Voltage
# ============================================================

voltage = yzero + ymult * (raw - yoff)


# ============================================================
# 8. 构造时间轴
#
# 先严格保留 Tek preamble 给出的绝对 trigger-relative 时间
# ============================================================

n = np.arange(len(raw))

time = xzero + xincr * (n - pt_off)


# 为画图方便转换成 μs
time_us = time * 1e6


# ============================================================
# 9. 保存原始数据 + calibration information
# ============================================================

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
np.savez(
    OUTPUT_DIR / "CH3_first_waveform.npz",

    raw=raw,
    voltage=voltage,
    time=time,

    xincr=xincr,
    xzero=xzero,
    pt_off=pt_off,

    ymult=ymult,
    yoff=yoff,
    yzero=yzero,

    byte_nr=byte_nr,
    bit_nr=bit_nr,
    record_length=record_length,
)


print("\nSaved:")
print(OUTPUT_DIR / "CH3_first_waveform.npz")


# ============================================================
# 10. 画波形
# ============================================================

plt.figure()

plt.plot(
    time_us,
    voltage
)

plt.xlabel("Time relative to trigger (µs)")
plt.ylabel("Voltage (V)")
plt.title("DPO70404C CH3")
plt.grid(True)

plt.tight_layout()
plt.show()


# ============================================================
# 11. 关闭通信
# ============================================================

scope.close()
rm.close()
