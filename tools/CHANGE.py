import pyvisa

rm = pyvisa.ResourceManager()

scope = rm.open_resource(
    "TCPIP0::192.168.1.7::inst0::INSTR"
)

scope.timeout = 5000

print("Instrument:")
print(scope.query("*IDN?").strip())

# 修改为 Real-Time sampling
scope.write("ACQUIRE:SAMPLINGMODE RT")

print("\nAfter switching to RT:")

print(
    "Sampling mode:",
    scope.query("ACQUIRE:SAMPLINGMODE?").strip()
)

print(
    "Interpolation ratio:",
    scope.query("HORIZONTAL:MAIN:INTERPRATIO?").strip()
)

print(
    "Sample rate:",
    scope.query("HORIZONTAL:MODE:SAMPLERATE?").strip()
)

print(
    "Record length:",
    scope.query("HORIZONTAL:MODE:RECORDLENGTH?").strip()
)

print(
    "Horizontal scale:",
    scope.query("HORIZONTAL:MODE:SCALE?").strip()
)

scope.close()
rm.close()