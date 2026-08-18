# DPO70404C scope computer

这个目录是 `CRgate` 实验中示波器电脑（DPO70404C）侧的程序、合同和本地实验数据。

## 目录结构

```text
.
├── formal_longitudinal_scope.py      # 正式纵场扫描 FastFrame 采集
├── precision_scope.py                # 时间/幅度精度扫描采集与分析
├── precision_contract_adapter.py     # 把 AWG 交接合同转成本地示波器合同
├── test_formal_longitudinal_scope.py
├── test_precision_scope.py
├── configs/                          # 小体积 JSON 合同与批配置
├── docs/                             # 实验协议、操作说明
├── tools/                            # 连接测试、单次读取、绘图等辅助脚本
├── presentations/                    # PPT 成品和构建/渲染文件
├── data/                             # 本地大文件：scope_runs、AWG handoff、CH3 原始波形
└── archive/                          # RAR、tmp、tmp_test 等不再直接使用的内容
```

`data/`、`archive/tmp*`、`*.rar`、`.venv/`、`node_modules/` 和
`configs/precision_scope_contract.local.json` 不提交 Git，只在示波器电脑本地保留。

## 快速运行

正式纵场采集：

```powershell
.\.venv\Scripts\python.exe formal_longitudinal_scope.py audit
.\.venv\Scripts\python.exe formal_longitudinal_scope.py preview --batch gatelen --shots-per-point 1
```

精度采集前，先用 AWG 交接合同生成本地合同：

```powershell
.\.venv\Scripts\python.exe precision_contract_adapter.py
```

然后使用 `configs/precision_scope_contract.local.json`：

```powershell
$cfg = Resolve-Path .\configs\precision_scope_contract.local.json
$cal = Resolve-Path .\data\scope_runs\formal-longitudinal-20260815_224030-9e200ecf\calibration\formal-longitudinal-20260816_110341-79304963-gatelen-L10-b01\attempt_001\scope_path_calibration.json

.\.venv\Scripts\python.exe .\precision_scope.py --config $cfg validate
.\.venv\Scripts\python.exe .\precision_scope.py --config $cfg arm --batch timing_precision_coarse --calibration-file $cal
```

更多细节见 [docs/README_FORMAL_SCOPE.md](docs/README_FORMAL_SCOPE.md) 和
[docs/README_PRECISION_SCOPE.md](docs/README_PRECISION_SCOPE.md)。
