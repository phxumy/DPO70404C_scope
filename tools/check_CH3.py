import pyvisa


# ============================================================
# 1. 连接 DPO70404C
# ============================================================

RESOURCE = "TCPIP0::192.168.1.7::inst0::INSTR"

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)

# VISA 通信超时：5 s
scope.timeout = 5000


# ============================================================
# 2. 确认连接的仪器
# ============================================================

print("=" * 60)
print("Instrument")
print("=" * 60)
print(scope.query("*IDN?").strip())


# ============================================================
# 3. 设置我们目前希望使用的采集模式
# ============================================================

# 关闭 FastAcq
scope.write("FASTACQ:STATE OFF")

# Real-Time sampling，不允许 IT 插值
scope.write("ACQUIRE:SAMPLINGMODE RT")

# 关闭 8-bit interpolation acquisition
scope.write("ACQUIRE:INTERPEIGHTBIT OFF")

# 普通 Sample 模式
scope.write("ACQUIRE:MODE SAMPLE")


# ============================================================
# 4. 设置 CH3
#
# 你现在的物理接线是：
#
#   AWG CH?+  ----------> 冰箱
#   AWG CH?-  ----------> 示波器 CH3
#
# 所以这里所有波形读取都使用 CH3
# ============================================================

# 打开 CH3 显示
scope.write("SELECT:CH3 ON")

# 50 Ω 输入
scope.write("CH3:TERMINATION 50")

# DC coupling
scope.write("CH3:COUPLING DC")

# 使用完整模拟带宽（DPO70404C 为 4 GHz）
scope.write("CH3:BANDWIDTH FULL")


# ============================================================
# 5. 指定之后要传输的 waveform 是 CH3
# ============================================================

scope.write("DATA:SOURCE CH3")


# ============================================================
# 6. 查询当前示波器整体采集参数
# ============================================================

print()
print("=" * 60)
print("Acquisition settings")
print("=" * 60)

queries = {
    "FastAcq":
        "FASTACQ:STATE?",

    "Sampling mode":
        "ACQUIRE:SAMPLINGMODE?",

    "Acquisition mode":
        "ACQUIRE:MODE?",

    "Actual acquisition mode":
        "ACQUIRE:MODE:ACTUAL?",

    "Interpolation ratio":
        "HORIZONTAL:MAIN:INTERPRATIO?",

    "Sample rate":
        "HORIZONTAL:MODE:SAMPLERATE?",

    "Record length":
        "HORIZONTAL:MODE:RECORDLENGTH?",

    "Horizontal scale":
        "HORIZONTAL:MODE:SCALE?",
}

for name, command in queries.items():
    try:
        value = scope.query(command).strip()
        print(f"{name:28s}: {value}")
    except Exception as e:
        print(f"{name:28s}: ERROR: {e}")


# ============================================================
# 7. 查询 CH3 设置
# ============================================================

print()
print("=" * 60)
print("CH3 settings")
print("=" * 60)

ch3_queries = {
    "CH3 termination":
        "CH3:TERMINATION?",

    "CH3 coupling":
        "CH3:COUPLING?",

    "CH3 bandwidth":
        "CH3:BANDWIDTH?",
}

for name, command in ch3_queries.items():
    try:
        value = scope.query(command).strip()
        print(f"{name:28s}: {value}")
    except Exception as e:
        print(f"{name:28s}: ERROR: {e}")


# ============================================================
# 8. 查询 CH3 waveform preamble
#
# 这些参数之后用于：
#
# t[n] = XZERO + XINCR * (n - PT_OFF)
#
# V[n] = YZERO + YMULT * (raw[n] - YOFF)
# ============================================================

print()
print("=" * 60)
print("CH3 waveform preamble")
print("=" * 60)

preamble_queries = {
    "XINCR":
        "WFMOUTPRE:XINCR?",

    "XZERO":
        "WFMOUTPRE:XZERO?",

    "PT_OFF":
        "WFMOUTPRE:PT_OFF?",

    "YMULT":
        "WFMOUTPRE:YMULT?",

    "YOFF":
        "WFMOUTPRE:YOFF?",

    "YZERO":
        "WFMOUTPRE:YZERO?",
}

for name, command in preamble_queries.items():
    try:
        value = scope.query(command).strip()
        print(f"{name:28s}: {value}")
    except Exception as e:
        print(f"{name:28s}: ERROR: {e}")


# ============================================================
# 9. 关闭连接
# ============================================================

scope.close()
rm.close()

print()
print("=" * 60)
print("Finished.")
print("=" * 60)