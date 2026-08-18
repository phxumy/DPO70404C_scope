import pyvisa

RESOURCE = "TCPIP0::192.168.1.7::inst0::INSTR"

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)
scope.timeout = 5000

print(scope.query("*IDN?").strip())

# ------------------------------------------------------------
# 设置 A Trigger 为普通 Edge Trigger
# ------------------------------------------------------------

scope.write("TRIGGER:A:TYPE EDGE")

# 触发源：正面的 AUX IN
scope.write("TRIGGER:A:EDGE:SOURCE AUXILIARY")

# MARK 脉冲通常看上升沿
scope.write("TRIGGER:A:EDGE:SLOPE:AUX RISE")

# 先用 TTL 预设触发电平
scope.write("TRIGGER:AUXLEVEL TTL")

# NORMAL：
# 没有真正的 AUX trigger 就不自动触发
scope.write("TRIGGER:A:MODE NORMAL")


# ------------------------------------------------------------
# 查询确认
# ------------------------------------------------------------

print()
print("Trigger source:",
      scope.query("TRIGGER:A:EDGE:SOURCE?").strip())

print("AUX slope:",
      scope.query("TRIGGER:A:EDGE:SLOPE:AUX?").strip())

print("AUX level:",
      scope.query("TRIGGER:AUXLEVEL?").strip())

print("Trigger mode:",
      scope.query("TRIGGER:A:MODE?").strip())

print("Trigger state:",
      scope.query("TRIGGER:STATE?").strip())

scope.close()
rm.close()