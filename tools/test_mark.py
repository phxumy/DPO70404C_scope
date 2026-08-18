import pyvisa

RESOURCE = "TCPIP0::192.168.1.7::inst0::INSTR"

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)
scope.timeout = 5000

print("=" * 60)
print(scope.query("*IDN?").strip())
print("=" * 60)

# ------------------------------------------------------------
# 基本采集模式
# ------------------------------------------------------------

scope.write("FASTACQ:STATE OFF")
scope.write("ACQUIRE:SAMPLINGMODE RT")
scope.write("ACQUIRE:MODE SAMPLE")

# ------------------------------------------------------------
# 打开 CH1 —— 现在 CH1 接的是 MARK5
# ------------------------------------------------------------

scope.write("SELECT:CH1 ON")
scope.write("CH1:COUPLING DC")
scope.write("CH1:BANDWIDTH FULL")

# 暂时不要主动修改 CH1 termination，
# 因为目前我们还没找到 MF-AWG-08 官方 MARK 输出阻抗规格
print("CH1 termination:",
      scope.query("CH1:TERMINATION?").strip())

# 先用 1 V/div，足够观察常见数字 marker
scope.write("CH1:SCALE 1.0")
scope.write("CH1:POSITION 0")

# ------------------------------------------------------------
# 非常重要：
# 清掉之前的水平 delay，把 trigger 放在屏幕中央
# ------------------------------------------------------------

scope.write("HORIZONTAL:DELAY:MODE OFF")
scope.write("HORIZONTAL:POSITION 50")

# 先看 100 ns/div，总屏幕约 1 us
scope.write("HORIZONTAL:MODE:SCALE 100E-9")

# ------------------------------------------------------------
# 用 CH1 本身作为触发源
# ------------------------------------------------------------

scope.write("TRIGGER:A:TYPE EDGE")
scope.write("TRIGGER:A:EDGE:SOURCE CH1")
scope.write("TRIGGER:A:EDGE:SLOPE:CH1 RISE")

# 先猜一个较低的 0.5 V 阈值。
# 只是为了找 MARK，不是最终标定值。
scope.write("TRIGGER:A:LEVEL:CH1 0.5")

scope.write("TRIGGER:A:MODE NORMAL")

# ------------------------------------------------------------
# 查询确认
# ------------------------------------------------------------

print()
print("Trigger source:",
      scope.query("TRIGGER:A:EDGE:SOURCE?").strip())

print("Trigger level CH1:",
      scope.query("TRIGGER:A:LEVEL:CH1?").strip())

print("Trigger state:",
      scope.query("TRIGGER:STATE?").strip())

print("Horizontal delay mode:",
      scope.query("HORIZONTAL:DELAY:MODE?").strip())

print("Horizontal position:",
      scope.query("HORIZONTAL:POSITION?").strip())

print("Horizontal scale:",
      scope.query("HORIZONTAL:MODE:SCALE?").strip())

scope.close()
rm.close()