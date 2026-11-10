# ESP32-S3 CSI 串口实时采集与数据集工具

把 ESP32-S3 通过串口（2000000 8N1）输出的 esp-csi CSV 数据**实时**采集为
深度学习数据集，方便 Python / MATLAB 处理与可视化。

```
csi_logger.py      主程序：实时读串口 → 数据集（自动选 CH343 串口，可选实时绘图）
collect_dataset.py 数据集采集驱动：按 session 动作表自动采集分类数据集（见第六节）
collect_dataset.sh 参考脚本：原 CSITool 版采集流程（仅作对照，本机不运行）
csi_parse.py       CSI_DATA 行解析库（被采集脚本共用）
csv_to_npz.py      离线转换：把已有抓包文本/raw.csv 转成数据集（可转 MobaXterm 导出）
plot_dataset.py    离线可视化 npz / 扁平 CSV 数据集
read_csi_matlab.m  MATLAB 加载数据集示例
pyproject.toml     uv 环境与依赖定义
csi_sessions/      csi_logger 单会话输出目录（自动创建）
csi_actions/       collect_dataset 数据输出目录（按动作分组，自动创建）
csi_log/           collect_dataset 标注日志与质量报告目录（自动创建）
.venv/             uv 创建的虚拟环境（自动创建，勿手动改）
```

## 一、环境管理（uv，仅限本目录）

本目录用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境与依赖，所有东西都收敛在
`serial/` 之内（`.venv/`、`uv.lock`），不影响项目的 ESP-IDF 环境。依赖定义见
`pyproject.toml`（pyserial / numpy / matplotlib / scipy / pyside6——最后者为
`--plot` 提供 GUI 后端，Windows 与 Linux 通用）。

```bat
:: Windows
:: 1. 安装 uv（已装可跳过；验证：uv --version）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

:: 2. 同步环境：首次自动创建 .venv 并安装全部依赖（含 Python 解释器）
cd /d D:\dev\esp-csi\serial
uv sync

:: 3. 之后运行任何脚本都用 uv run（自动使用 .venv）
uv run python csi_logger.py --help
```

```bash
# Linux（Ubuntu 等）
# 1. 安装 uv（已装可跳过；验证：uv --version）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步环境：首次自动创建 .venv 并安装全部依赖
cd ~/dev/esp-csi/serial
uv sync

# 3. 之后运行任何脚本都用 uv run（自动使用 .venv）
uv run python csi_logger.py --help
```

> **Linux 串口权限**：板子在 Ubuntu 上挂为 `/dev/ttyACM0`（CH343 CDC 模式），
> 默认属组为 `dialout`，需把自己加进该组并重新登录后才能打开串口：
> `sudo usermod -aG dialout $USER`（查看：`id -nG | grep dialout`）。

依赖变更后（改 pyproject.toml）再次 `uv sync` 即可。也可用 `uv add 包名` /
`uv remove 包名` 直接增删。

## 二、实时采集

```bat
:: 1. 自动选择 CH343 串口并采集（推荐先用 --list-ports 确认）
uv run python csi_logger.py --list-ports

:: 2. 开始采集（屏幕只显示统计，不打印原始数据；q 或 Ctrl-C 结束）
uv run python csi_logger.py

:: 3. 带实时绘图（HT-LTF 瀑布图 + 当前包子载波幅度曲线，宽度自适应）
uv run python csi_logger.py --plot

:: 4. 采集分类数据集：设置标签面板，按数字键 0/1/2 实时切换当前标签
uv run python csi_logger.py --labels 空房间,走路,挥手

:: 5. 定时 60 秒自动结束 + 额外输出 .mat
uv run python csi_logger.py --duration 60 --mat
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--port COMx` | 手动指定串口（Windows: `COMx`；Linux: `/dev/ttyACM0` 等；默认自动选 CH343/CH340/CH341 口——Linux 上 CH343 走 CDC-ACM 挂为 `ttyACM*`，按 VID 1a86 识别；多个候选时逐个探测 2s，选真正在发 CSI_DATA 的口） |
| `--label 名称` | 本次采集的初始标签（写入文件名和 label 列） |
| `--labels a,b,c` | 标签面板；采集中按数字键 0-9 切换（逐包记录在 label 列）。Windows/Linux 均支持（Linux 需在前台终端运行） |
| `--duration 秒` | 定时结束（0 = 手动 Ctrl-C/q） |
| `--plot` | 实时绘图（瀑布图为 HT-LTF 段幅度） |
| `--all-ltfs` | npz/mat 不做段裁剪：三段固件（len=384）保存全部三段（192 子载波）；单段固件（len=128/256）数据本就完整，此参数无效果 |
| `--mat` | 结束时额外保存 `.mat`（需 scipy） |
| `--save-interval 秒` | 快照间隔，默认 5s；越大越省 CPU |
| `--out-dir 路径` | 输出目录，默认 `serial/csi_sessions/` |

注意：采集前请**关闭 MobaXterm** 等其他占用该串口的程序，否则端口打开会失败。

## 三、输出数据集格式

每次采集生成一组文件（`csi_<时间>_<标签>.*`，N = 有效包数）：

| 文件 | 用途 | 读取方式 |
| --- | --- | --- |
| `*_raw.csv` | 原始 CSI_DATA 行透传（含表头，与 MobaXterm 抓包一致），归档用 | `python csv_to_npz.py xxx_raw.csv` 可再转成下面两种格式 |
| `*_data.csv` | 扁平数值表：12 列元数据（seq, host_time, t_rel, label, id, rssi, channel, sec_channel, local_timestamp_us, sig_len, len, first_word）+ `len×?` 列 `iq_XXXX`（三段固件 384 列，单段固件按实际长度） | MATLAB `readmatrix`、pandas `read_csv`、`np.genfromtxt` 直接读 |
| `*_data.npz` | numpy 存档（深度学习训练主格式；宽度按固件上报内容自适应，见下方"子载波布局"） | Python: `np.load` |
| `*_data.mat` | （`--mat` 时）MATLAB 原生格式 | MATLAB: `load` |

**npz 内容**（MATLAB 用户看 `.mat`，键名相同）：

| 键 | 形状 | 说明 |
| --- | --- | --- |
| `csi` | N×64（三段固件默认）/ N×128（HT-LTF 原生固件）/ N×192（三段固件 + `--all-ltfs`） | 复数子载波 |
| `amp` / `phase` | 同 `csi` | 幅度 / 相位（可直接喂给网络） |
| `iq` | 与 `csi` 宽度对应（×2 字节：64→128 / 128→256 / 192→384） | 固件原始值：偶数下标=虚部Q，奇数下标=实部I（乘补偿增益后可能超 ±127） |
| `id`, `rssi`, `channel`, `sec_channel`, `local_timestamp_us`, `sig_len`, `first_word` | N | 逐包元数据（对应 CSV 各列） |
| `host_time` / `t_rel` | N | 电脑时间戳（epoch 秒，按包到达时刻打点）/ 相对开始秒数 |
| `label` | N | 每包标签（按键切换时逐包记录） |
| `ltf_segments` / `iq_layout` | - | 自描述字符串：本文件 csi 的段布局与 iq 排列约定 |

**子载波布局**（按每包 `len` 自适应）：
* `len=384`（三段固件）：`csi[:, 0:64]` = LLTF，`csi[:, 64:128]` = HT-LTF，`csi[:, 128:192]` = STBC-HT-LTF。npz **默认只保存 HT-LTF 段**（`csi[:, 0:64]` 即 HT-LTF）；`--all-ltfs` 保存全部三段。
* `len=256`（固件仅开 `htltf_en`）：csi 为 **HT-LTF 原生 128 点频谱**（40MHz，114 个有效数据子载波，分辨率高于三段格式），npz 原样保存。
* `len=128`（固件仅开 `lltf_en` 等其他单段组合）：数据即完整 buf，npz 原样保存。

各情况 `ltf_segments` 键均有文字说明。`*_raw.csv` 与 `*_data.csv` 恒为完整数据，需要时可再转换。

Python 加载示例：

```python
import numpy as np
d = np.load('csi_sessions/csi_xxx_data.npz')
amp, labels = d['amp'], d['label']      # 直接可用于训练
# 单独转 raw.csv: python csv_to_npz.py xxx_raw.csv --label 走路
```

## 四、离线转换历史抓包

MobaXterm 导出文本（含日志行）可直接转换：

```bat
uv run python csv_to_npz.py MobaXterm_SerialCOM_20261103_100527.txt --label 静止
uv run python csv_to_npz.py a_raw.csv b_raw.csv --out-dir csi_sessions
uv run python csv_to_npz.py xxx.txt --all-ltfs   # 需要全部三段时
```

## 五、可视化

```bat
uv run python plot_dataset.py csi_sessions/csi_xxx_data.npz            # 弹窗: 热力图+幅度/相位时序
uv run python plot_dataset.py csi_sessions/csi_xxx_data.npz --save a.png
uv run python plot_dataset.py csi_sessions/csi_xxx_data.csv            # 扁平 CSV 同样支持
```

MATLAB 用法见 `read_csi_matlab.m`。

## 六、数据集采集（行为检测流程，复刻 collect_dataset.sh）

`collect_dataset.py` 按 session 动作表自动采集分类数据集（原 CSITool 脚本的
核心流程与标注规范的 ESP32 复刻；无需连 WiFi/hping3，发送板自带流量）：

| Session | 内容 | 标签 |
| --- | --- | --- |
| `session_A` | 13 段 × 3s `emptyroom`（0 人基线） | 无 label |
| `session_B` | 1 人 13 段 × 3s：`standstill/walk/sit/standup/wave/bend/jump` + `kicksim1-3/punchsim1-3` | 前者 label=0，后者 label=1 |
| `session_C` | 2 人 11 段 × 3s：`approach/leave/handshake/highfive/pointleft/pointright` + `push/kickleft/kickright/punchleft/punchright` | 同上 |
| `session_D` | 人数检测：1 段 × 30s，人数为命令行参数（1-10） | 无 label |

```bat
uv run python collect_dataset.py session_A          :: 先跑空房间基线
uv run python collect_dataset.py session_B          :: 单人 HAR + 暴力模拟
uv run python collect_dataset.py -r 3 session_B     :: 重复 3 次
uv run python collect_dataset.py session_D 3        :: 3 人检测
uv run python collect_dataset.py session_B --plot   :: 带实时绘图（瀑布图）
```

每个**动作**生成一组独立文件（`csi_<时间戳>_<session>_<5位序号>_<动作名>` 前缀，
npz/raw.csv/data.csv），输出到 `csi_actions/`；每 run 一个标注日志
`csi_log/csi_<时间戳>_<session>_<run>_annotation.log`，**保持原脚本格式**：

```text
session_start, 1762314612.123, session_B
action_start, 1762314613.456, standstill, people=1, label=0, dat_file=csi_20261105_143013_session_B_00001_standstill
action_end,   1762314616.456, standstill, people=1, label=0, dat_file=csi_20261105_143013_session_B_00001_standstill
...
people_change_start, 1762314700.000, people=3, dat_file=csi_..._00001_peoplecount
session_end, 1762314710.000, session_B
```

时间同步与质量保证：

* **串口整个进程只打开一次**；动作切换是进程内毫秒级换文件组，屏幕计时与实际
  采集起点同步。每包自带 `host_time/t_rel` 与固件 `local_timestamp_us` 两组
  时间戳，离线可按包时间戳精确对齐；段间到达的数据直接丢弃。
* 每动作前大字提示（动作/时长/人数）停留 `--prompt-time` 秒（默认 1，与参考脚本一致）。
* **实时告警**：固件时间戳间隔超 `--gap-threshold`（默认 0.5s）→ 空窗告警；
  串口 3 秒无数据 → 链路告警。
* **段末质量报告**（打印 + 追加到 `csi_log/quality_report.csv`）：包数、平均
  采样率、间隔统计（均值/中位/P95/最大/标准差）、id 断号数；三项判定——采样率
  ≥ `--expected-rate`（默认 100）的 80%、最大间隔 ≤ 阈值、P95 ≤ 2×中位（均匀性）。
* npz 内容按固件上报自适应：三段固件默认仅存 HT-LTF 段（64 子载波），HT-LTF
  原生固件（len=256）为完整 128 子载波；`--all-ltfs` 不裁剪。`--mat` 每动作另存 .mat。

建议每个采集日先跑 `session_A`（串口首开的固件增益基线约 100 包，由它覆盖）。

## 七、常见问题

* **采样率低于发送率、质量报告显示"主机到达率 ≈ 固件采样率"且 id断号 较多**：
  固件串口打印带宽上限（打印为阻塞式，打印期间新 CSI 事件被固件丢弃）。
  921600 波特 ≈ 92KB/s，上限随每行字节数变化：三段固件（~1.3KB）→ ~71Hz；
  仅 HT-LTF（len=256，~1.0KB）→ ~91Hz；仅 LLTF（len=128，~0.6KB）→ ~160Hz。
  解决：`idf.py menuconfig` → `Component config` → `ESP-STDIO` →
  `UART console baud rate` 提到 `2000000`（上限 ~190Hz；注意 IDF v6.1 该选项
  在 ESP-STDIO 菜单，不在 ESP System Settings），重新烧录接收板（采集端
  `--baud 2000000`，现已是默认值）。
* **串口打开失败**：端口被占用（关掉 MobaXterm 等），或口不对（`--list-ports` 查看，`--port` 指定）。
  Linux 下报 `Permission denied`：把自己加入 `dialout` 组并重新登录
  （`sudo usermod -aG dialout $USER`）。
* **`--plot` 无窗口（Linux）**：uv 管理的 Python 不带 tkinter 时 matplotlib 退到
  无窗口的 Agg 后端。`uv sync` 安装 pyside6 后自动用 QtAgg；若报 Qt 平台插件
  错误（`xcb` 找不到），补系统库：`sudo apt install libxcb-cursor0`。
* **Linux 下 `--list-ports` 列出大量 ttyS***：主板遗留串口，非 USB 设备，忽略即可；
  CH34x 已自动排在最前并带 `[CH34x]` 标记。
* **包速率偏低/积压警告**：处理速度跟不上。建议关掉 `--plot`、加大 `--save-interval`；固件日志（compensate_gain 行）较多属正常，已自动过滤。
* **多块板子同时插着**：程序会自动探测每块板是否在发 CSI 数据（各等 2s），选中的口会打印出来；也可 `--port` 指定。
* **为什么 raw.csv 和 data.csv 各一份**：raw.csv 忠实存档原始行（含被丢弃的坏行，便于排查），data.csv 是清洗后的数值表。
