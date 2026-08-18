import pyvisa

rm = pyvisa.ResourceManager()

print(rm.list_resources())

scope = rm.open_resource(
    "TCPIP0::192.168.1.7::inst0::INSTR"
)

print(scope.query("*IDN?"))