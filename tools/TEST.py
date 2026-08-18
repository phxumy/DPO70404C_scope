import pyvisa

rm = pyvisa.ResourceManager()

scope = rm.open_resource(
    "TCPIP0::192.168.1.7::inst0::INSTR"
)

scope.timeout = 5000

print("Instrument:")
print(scope.query("*IDN?").strip())

queries = {
    "FastAcq": "FASTACQ:STATE?",
    "Sampling mode": "ACQUIRE:SAMPLINGMODE?",
    "Acquisition mode": "ACQUIRE:MODE?",
    "Actual acquisition mode": "ACQUIRE:MODE:ACTUAL?",
    "Interpolation ratio": "HORIZONTAL:MAIN:INTERPRATIO?",
    "Sample rate": "HORIZONTAL:MODE:SAMPLERATE?",
    "Record length": "HORIZONTAL:MODE:RECORDLENGTH?",

    "CH1 termination": "CH1:TERMINATION?",
    "CH1 coupling": "CH1:COUPLING?",
    "CH1 bandwidth": "CH1:BANDWIDTH?",
}

print("\nCurrent settings:")

for name, command in queries.items():
    try:
        value = scope.query(command).strip()
        print(f"{name:25s}: {value}")
    except Exception as e:
        print(f"{name:25s}: ERROR: {e}")

scope.close()
rm.close()