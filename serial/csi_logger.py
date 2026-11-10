# -*- coding: utf-8 -*-
"""csi_logger.py — 实时采集 ESP32-S3 串口 CSI 数据并保存为深度学习数据集。

功能：
  * 自动选择串口：优先匹配设备管理器中的 "USB-Enhanced-SERIAL CH343"
    （兼容 CH340/CH341）；存在多个候选时逐个探测，选出真正在发
    CSI_DATA 数据的那个。也可用 --port COMx 手动指定。
  * 高速读取：独立读线程只做「读字节 → 切行 → 入队」，解析/落盘/绘图
    都在主线程完成，避免处理卡顿导致串口缓冲区溢出丢数据。
  * 数据集输出（默认 serial/csi_sessions/csi_<时间>_<标签>.*）：
      *_raw.csv   原始 CSI_DATA 行透传（与 MobaXterm 抓包格式一致）
      *_data.csv  扁平数值表（MATLAB readmatrix / pandas 直接可读，恒为全部数据）
      *_data.npz  numpy 存档（深度学习训练主格式，Python: np.load）
                  宽度按固件上报自适应：三段固件(len=384)默认仅存 HT-LTF 段；
                  单段固件(len=128/256)为完整数据；--all-ltfs 不裁剪
      *_data.mat  --mat 可选，MATLAB: load 直接读取（需要 scipy）
  * 采集分类数据集：--labels 空房间,走路,挥手 设置标签面板，
    按数字键 0-9 实时切换当前标签（逐包记录在 label 列），q 结束。
  * --plot 实时绘图（幅度瀑布图 + 当前包子载波幅度曲线，宽度自适应）。

用法示例：
  python csi_logger.py                      # 自动找口，开始采集
  python csi_logger.py --plot               # 带实时绘图
  python csi_logger.py --labels 空房间,走路  # 按键 0/1 切换标签
  python csi_logger.py --port COM5 --duration 60

屏幕上只显示统计状态，不打印原始数据。
"""

import argparse
import os
import queue
import sys
import threading
import time

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print('缺少 pyserial，请先安装：pip install pyserial')
    sys.exit(1)

try:
    import msvcrt  # Windows 键盘非阻塞读取
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

try:
    import atexit
    import select as _select
    import termios
    import tty
    HAS_TERMIOS = True   # POSIX 键盘非阻塞读取（Linux/macOS）
except ImportError:
    HAS_TERMIOS = False

# 让本目录下的 csi_parse 在任意工作目录下都能导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi_parse import HEADER_LINE, LTF_SEGMENTS, is_csi_line, parse_csi_line

DEFAULT_BAUD = 2000000
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'csi_sessions')
PLOT_REFRESH = 0.25     # 实时绘图刷新周期（秒）
PLOT_HISTORY = 200      # 热力图保留的包数


# ---------------------------------------------------------------- 串口选择

WCH_VID = 0x1a86          # 沁恒（WCH）USB VID：CH340/CH341/CH343

def _is_ch34x(port):
    """识别 CH34x 串口。

    Windows 按描述串匹配（"USB-Enhanced-SERIAL CH343"）；Linux 下 CH343
    （VID:PID 1a86:55d3）由 cdc_acm 驱动挂为 /dev/ttyACM0，描述为
    "USB Single Serial"，不含 CH343 字样，故同时按 VID 匹配。
    """
    if getattr(port, 'vid', None) == WCH_VID:
        return True
    desc = (port.description or '').upper()
    return any(k in desc for k in ('CH343', 'CH340', 'CH341'))


def list_all_ports():
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)


def _probe_csi(device, baud, timeout_s=2.0):
    """打开串口嗅探 timeout_s 秒，判断是否在输出 CSI_DATA 数据。"""
    try:
        with serial.Serial(device, baud, bytesize=8, parity='N',
                           stopbits=1, timeout=0.1) as ser:
            ser.reset_input_buffer()
            buf = b''
            t0 = time.time()
            while time.time() - t0 < timeout_s:
                buf += ser.read(4096)
                if b'CSI_DATA' in buf:
                    return True
    except (serial.SerialException, OSError):
        return False
    return False


def autodetect_port(baud, probe_timeout):
    """自动选择串口：CH34x 候选口；多个时探测出真正发 CSI_DATA 的那个。"""
    candidates = [p for p in list_all_ports() if _is_ch34x(p)]
    if not candidates:
        print('未找到 CH343/CH340/CH341 串口设备。当前系统串口：')
        for p in list_all_ports():
            print(f'  {p.device:10s} {p.description}')
        print('请插好开发板，或用 --port 手动指定（Windows: COMx；'
              'Linux: /dev/ttyACM0 等）。')
        sys.exit(1)

    if len(candidates) == 1:
        print(f'检测到串口 {candidates[0].device}（{candidates[0].description}）')
        return candidates[0].device

    print(f'检测到 {len(candidates)} 个 CH34x 串口，探测哪个在输出 CSI 数据'
          f'（每个最多等 {probe_timeout:.0f}s）...')
    for p in candidates:
        print(f'  探测 {p.device} ...', end='', flush=True)
        if _probe_csi(p.device, baud, probe_timeout):
            print(' 收到 CSI_DATA ✓')
            return p.device
        print(' 无数据')
    print('没有端口探测到 CSI 数据，使用第一个候选口（可用 --port 覆盖）。')
    return candidates[0].device


# ---------------------------------------------------------------- 键盘输入

def make_key_reader():
    """构造非阻塞读键函数（有键→1 字符 str，无键→None）；不可用时返回 None。

    Windows 走 msvcrt；Linux/macOS 走 termios cbreak + select，行为一致
    （数字键切标签 / q 结束；cbreak 下 Ctrl-C 仍可中断）。stdin 不是终端
    （重定向/管道）时禁用按键，不影响正常采集。
    """
    if HAS_MSVCRT:
        def read_key():
            if msvcrt.kbhit():
                return msvcrt.getwch()
            return None
        return read_key

    if HAS_TERMIOS:
        try:
            if not sys.stdin.isatty():
                return None
            fd = sys.stdin.fileno()
            old_attr = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, ValueError, OSError):
            return None

        def restore():
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            except Exception:
                pass
        atexit.register(restore)

        def read_key():
            ready, _, _ = _select.select([fd], [], [], 0)
            if not ready:
                return None
            # os.read 直读 fd，绕开 TextIOWrapper 缓冲；select 已确认有数据，
            # cbreak 模式下不会阻塞。一次取首字符（多键连击余下丢弃，无碍）。
            return os.read(fd, 64).decode('utf-8', 'ignore')[:1] or None
        return read_key

    return None


# ---------------------------------------------------------------- 数据落盘

class DatasetWriter:
    """一次采集会话的四种输出文件与内存缓冲。"""

    FLAT_META_COLUMNS = ['seq', 'host_time', 't_rel', 'label', 'id', 'rssi',
                         'channel', 'sec_channel', 'local_timestamp_us',
                         'sig_len', 'len', 'first_word']

    def __init__(self, out_dir, stem, write_flat=True, all_ltfs=False):
        os.makedirs(out_dir, exist_ok=True)
        self.n = 0
        self.mac = ''
        self.all_ltfs = all_ltfs

        self.path_raw = os.path.join(out_dir, stem + '_raw.csv')
        self.path_flat = os.path.join(out_dir, stem + '_data.csv')
        self.path_npz = os.path.join(out_dir, stem + '_data.npz')

        # 1) 原始行透传
        self._f_raw = open(self.path_raw, 'w', encoding='ascii', newline='\n')
        self._f_raw.write(HEADER_LINE + '\n')

        # 2) 扁平数值表（MATLAB/pandas 友好）
        self.write_flat = write_flat
        if write_flat:
            self._f_flat = open(self.path_flat, 'w', encoding='utf-8', newline='\n')
            cols = self.FLAT_META_COLUMNS + [f'iq_{i:04d}' for i in range(384)]
            self._f_flat.write(','.join(cols) + '\n')

        # 3) npz / 4) mat 的内存缓冲
        self.csi_width = None            # 每包复数子载波数（首包确定）
        self.ids, self.rssi, self.channels, self.sec_channels = [], [], [], []
        self.ts_us, self.sig_len, self.first_word = [], [], []
        self.host_time, self.t_rel, self.labels = [], [], []
        self.iq_rows, self.csi_rows = [], []

    def add(self, pkt, label, host_time, t_rel):
        """记录一个已成功解析的 CSI 包。"""
        if self.csi_width is None:
            self.csi_width = pkt['csi'].size
        self.mac = pkt['mac']
        self.ids.append(pkt['id'])
        self.rssi.append(pkt['rssi'])
        self.channels.append(pkt['channel'])
        self.sec_channels.append(pkt['sec_channel'])
        self.ts_us.append(pkt['timestamp_us'])
        self.sig_len.append(pkt['sig_len'])
        self.first_word.append(pkt['first_word'])
        self.host_time.append(host_time)
        self.t_rel.append(t_rel)
        self.labels.append(label)
        iq = pkt['iq']
        self.iq_rows.append(iq)
        self.csi_rows.append(pkt['csi'])
        self.n += 1

        if self.write_flat:
            meta = (f'{self.n - 1},{host_time:.6f},{t_rel:.6f},{label},{pkt["id"]},'
                    f'{pkt["rssi"]},{pkt["channel"]},{pkt["sec_channel"]},'
                    f'{pkt["timestamp_us"]},{pkt["sig_len"]},{iq.size},'
                    f'{pkt["first_word"]}')
            self._f_flat.write(meta + ',' + ','.join(map(str, iq.tolist())) + '\n')

    def write_raw_line(self, text):
        """原始行透传（含解析失败的 CSI 行，便于事后检查）。"""
        self._f_raw.write(text.strip() + '\n')

    def _arrays(self):
        width = self.csi_width or 192
        csi_full = (np.stack(self.csi_rows) if self.csi_rows
                    else np.zeros((0, width), np.complex64))
        iq_full = (np.stack(self.iq_rows) if self.iq_rows
                   else np.zeros((0, width * 2), np.int16))
        if width == 192 and not self.all_ltfs:
            # 三段布局（len=384）：默认只保留 HT-LTF 段（训练推荐），复数下标 64..127
            csi, iq = csi_full[:, 64:128], iq_full[:, 128:256]
            seg_note = ('csi[:,0:64]=HT-LTF (默认仅保存HT-LTF段, '
                        '--all-ltfs 保存全部三段)')
        elif width == 192:
            csi, iq = csi_full, iq_full
            seg_note = ('csi[:,0:64]=LLTF, csi[:,64:128]=HT-LTF, '
                        'csi[:,128:192]=STBC-HT-LTF')
        elif width == 128:
            # 固件仅开 htltf_en（len=256）：HT-LTF 原生 128 点频谱
            csi, iq = csi_full, iq_full
            seg_note = ('csi=HT-LTF 原生频谱（40MHz HT-LTF 128 点 FFT，114 有效'
                        '子载波；固件仅开 htltf_en 时上报）')
        else:
            # 其他单段布局：数据即完整 buf
            csi, iq = csi_full, iq_full
            seg_note = (f'csi=完整 buf（每包 {width} 复数，固件单段 LTF；'
                        '段别由固件 lltf_en/htltf_en 配置决定）')
        return {
            'csi': csi,                                   # complex64
            'amp': np.abs(csi).astype(np.float32),        # 幅度
            'phase': np.angle(csi).astype(np.float32),    # 相位
            'iq': iq,                                     # int16 原始值
            'id': np.asarray(self.ids, np.int32),
            'rssi': np.asarray(self.rssi, np.int16),
            'channel': np.asarray(self.channels, np.int16),
            'sec_channel': np.asarray(self.sec_channels, np.int16),
            'local_timestamp_us': np.asarray(self.ts_us, np.int64),
            'sig_len': np.asarray(self.sig_len, np.int16),
            'first_word': np.asarray(self.first_word, np.int8),
            'host_time': np.asarray(self.host_time, np.float64),
            't_rel': np.asarray(self.t_rel, np.float64),
            'label': np.asarray(self.labels),
            'mac': self.mac,
            'ltf_segments': seg_note,
            'iq_layout': 'iq 偶数下标=虚部Q, 奇数下标=实部I (先虚后实)',
        }

    def snapshot(self):
        """增量落盘：刷新文本文件 + 重写 npz（先写临时文件再替换，避免半截文件）。"""
        self._f_raw.flush()
        if self.write_flat:
            self._f_flat.flush()
        if self.n == 0:
            return
        path_tmp = self.path_npz + '.tmp.npz'
        np.savez_compressed(path_tmp, **self._arrays())
        os.replace(path_tmp, self.path_npz)

    def finalize(self, save_mat=False):
        """结束会话：最后一次快照，可选输出 .mat，关闭文件。"""
        self.snapshot()
        mat_path = None
        if save_mat and self.n > 0:
            try:
                from scipy.io import savemat
            except ImportError:
                print('提示：未安装 scipy，跳过 .mat 输出（pip install scipy）')
            else:
                mat_path = self.path_npz[:-4] + '.mat'
                data = self._arrays()
                # MATLAB 友好：一维数值元数据存为列向量，字符串标签存为元胞数组
                m = {k: np.asarray(v).reshape(-1, 1) if np.ndim(v) == 1 else v
                     for k, v in data.items()
                     if k not in ('label', 'mac', 'ltf_segments', 'iq_layout')}
                m['label'] = np.asarray(self.labels, dtype=object).reshape(-1, 1)
                m['mac'] = self.mac
                savemat(mat_path, m, do_compression=True)
        self._f_raw.close()
        if self.write_flat:
            self._f_flat.close()
        return mat_path


# ---------------------------------------------------------------- 实时绘图

GUI_BACKENDS = ('QtAgg', 'TkAgg')   # 依次尝试的交互式后端


def _use_gui_backend():
    """确保 matplotlib 使用交互式 GUI 后端（--plot 必须有窗口）。

    桌面环境下 matplotlib 通常已自动选中 TkAgg/QtAgg，直接返回；退回 Agg
    （如 uv 管理的 Python 不带 tkinter）时，按候选顺序检测绑定库并切换。
    全部缺失则抛错（open() 会捕获并降级为无绘图继续采集）。
    """
    import matplotlib
    if matplotlib.get_backend().lower() != 'agg':
        return
    for backend in GUI_BACKENDS:
        try:
            if backend == 'QtAgg':
                try:
                    import PySide6  # noqa: F401
                except ImportError:
                    import PyQt6    # noqa: F401
            else:
                import tkinter  # noqa: F401
        except ImportError:
            continue
        matplotlib.use(backend)
        return
    raise RuntimeError('无可用 GUI 后端（matplotlib 仅有无窗口的 Agg）。'
                       '请在 serial/ 下执行 uv sync 安装 pyside6，'
                       '或使用系统 Python（自带 tkinter）。')


class LivePlot:
    """实时显示：上=最近若干包子载波幅度瀑布图，下=当前包幅度曲线（宽度自适应）。"""

    def __init__(self):
        _use_gui_backend()
        import matplotlib.pyplot as plt
        self.plt = plt
        plt.ion()
        self.fig, (self.ax_hm, self.ax_ln) = plt.subplots(
            2, 1, figsize=(9, 6), constrained_layout=True)

        self.hist = np.zeros((PLOT_HISTORY, 64), np.float32)   # 占位，宽度在首包后自适应
        self.im = self.ax_hm.imshow(self.hist, aspect='auto', interpolation='nearest',
                                    cmap='viridis', vmin=0, vmax=100)
        self.ax_hm.set_title('HT-LTF amplitude waterfall (recent packets)')
        self.ax_hm.set_xlabel('subcarrier bin')
        self.ax_hm.set_ylabel('packet (old -> new)')
        self.fig.colorbar(self.im, ax=self.ax_hm)

        self.amp_now = np.zeros(192, np.float32)
        self.lines = []
        self._line_width = None         # 首包确定宽度后重建下方曲线
        self._wf_width = None           # 瀑布图行宽（三段=64 HT-LTF 段，单段=全宽）
        self.ax_ln.set_xlim(0, 192)
        self.ax_ln.set_ylim(0, 100)
        self.ax_ln.set_xlabel('subcarrier index')
        self.ax_ln.set_ylabel('|CSI|')
        self.txt = self.ax_ln.text(0.01, 0.92, '', transform=self.ax_ln.transAxes,
                                   fontsize=8)

    def _build_lines(self):
        for ln in self.lines:
            ln.remove()
        self.lines = []
        if self._line_width >= 192:
            colors = ('tab:blue', 'tab:orange', 'tab:green')
            for k, (a, b, name) in enumerate(LTF_SEGMENTS):
                (ln,) = self.ax_ln.plot(range(a, b), np.zeros(b - a),
                                        color=colors[k], label=f'{name} [{a}:{b})')
                self.lines.append(ln)
        else:
            (ln,) = self.ax_ln.plot(range(self._line_width),
                                    np.zeros(self._line_width), color='tab:blue',
                                    label=f'LTF (single segment, {self._line_width})')
            self.lines = [ln]
        self.ax_ln.set_xlim(0, self._line_width)
        self.ax_ln.legend(loc='upper right', fontsize=8)

    def push(self, csi):
        """主线程每收到一包调用一次（宽度自适应：三段 192 或单段 64/128）。"""
        csi = np.asarray(csi, np.complex64)
        self.amp_now = np.abs(csi)
        if self._line_width is None:
            self._line_width = self.amp_now.size
            self._wf_width = 64 if self._line_width >= 192 else self._line_width
            self._build_lines()
            # 瀑布图缓冲与瀑布行同宽（首包确定）
            self.hist = np.zeros((PLOT_HISTORY, self._wf_width), np.float32)
        if self._line_width >= 192:
            row = self.amp_now[64:128]      # 三段布局：瀑布图画 HT-LTF 段
        else:                               # 单段布局：瀑布图画整段（与下方对齐）
            row = self.amp_now
        w = self._wf_width
        if row.size < w:
            row = np.pad(row, (0, w - row.size))
        elif row.size > w:
            row = row[:w]
        self.hist = np.vstack([self.hist[1:], row[None, :]])

    def refresh(self, rssi):
        vmax = max(10.0, float(self.hist.max()))
        self.im.set_data(self.hist)
        self.im.set_clim(0, vmax)
        if self._line_width is not None and self._line_width >= 192:
            for k, (a, b, _) in enumerate(LTF_SEGMENTS):
                self.lines[k].set_ydata(self.amp_now[a:b])
        elif self.lines:
            self.lines[0].set_ydata(self.amp_now[:self._line_width])
        self.ax_ln.set_ylim(0, max(10.0, float(self.amp_now.max()) * 1.1))
        self.txt.set_text(f'RSSI {rssi} dBm')
        self.plt.pause(0.001)

    def close(self):
        self.plt.ioff()
        self.plt.close(self.fig)


# ---------------------------------------------------------------- 读线程

def reader_loop(ser, q, stop_event):
    """读线程：读字节 → 按 \\n 切行 → 打上到达时间戳 → 入队。

    高频运行，保证串口驱动缓冲从不过期；到达时间戳用于 host_time/t_rel，
    使其反映每个包的真实到达时刻（不受主线程批处理节奏影响）。
    """
    carry = bytearray()
    while not stop_event.is_set():
        try:
            chunk = ser.read(4096)   # 大块读：减少 GIL 竞争与系统调用次数
        except (serial.SerialException, OSError):
            break
        if not chunk:
            continue
        carry += chunk
        while True:
            idx = carry.find(b'\n')
            if idx < 0:
                if len(carry) > 65536:   # 异常防护：长时间无换行的垃圾数据
                    carry.clear()
                break
            q.put((bytes(carry[:idx]), time.time()))
            del carry[:idx + 1]


# ---------------------------------------------------------------- 采集会话

class CsiCollector:
    """可编程 CSI 采集会话：串口只开一次，运行中毫秒级切换输出文件组。

    csi_logger.py 命令行模式与 collect_dataset.py 逐动作录制共用。
    """

    def __init__(self, device=None, baud=DEFAULT_BAUD, probe_timeout=2.0,
                 all_ltfs=False, save_mat=False, save_interval=5.0,
                 gap_threshold=0.5, expected_rate=100.0, plot=False,
                 write_flat=True):
        self.device = device            # None = open() 时自动选口
        self.baud = baud
        self.probe_timeout = probe_timeout
        self.all_ltfs = all_ltfs
        self.save_mat = save_mat
        self.save_interval = save_interval
        self.gap_threshold = gap_threshold
        self.expected_rate = expected_rate
        self.plot_enabled = plot
        self.write_flat = write_flat

        self.ser = None
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.reader = None
        self.plot = None
        self.writer = None              # 当前段 DatasetWriter
        self.current_stem = None
        self.current_label = 'data'
        self.writer_dir = DEFAULT_OUT_DIR
        self.t_start = None             # open() 时刻，t_rel 基准
        self.last_files = None          # 最近一次 finalize 的文件路径与包数

        # 段内统计（end_segment 汇总后重置）
        self.n_packets = 0
        self.n_invalid = 0
        self.n_log = 0
        self.n_id_gaps = 0              # id 断号损失包数
        self.n_time_gaps = 0            # 固件时钟空窗次数（间隔 > gap_threshold）
        self.n_host_gaps = 0            # 主机到达空窗次数
        self.last_pkt_time = None       # 主机时钟：最近一个有效包（链路健康检查）
        self._fw_ts = None
        self._host_ts = None
        self._last_id = None
        self._intervals = []
        self._host_intervals = []
        self._t_last_save = 0.0
        self._t_last_plot = 0.0
        self._warned_backlog = False
        self._time_gap_warned = False
        # 全程累计
        self.total_packets = 0
        self.total_id_gaps = 0

    # -- 生命周期 ----------------------------------------------
    def open(self):
        """打开串口并启动读线程（整个采集进程只调用一次）。"""
        if self.device is None:
            self.device = autodetect_port(self.baud, self.probe_timeout)
        # 绘图窗口先于串口创建：matplotlib 导入/建窗/首次渲染耗时可达秒级，
        # 其间 GIL 被长时间占用，会饿死读线程；先建窗时还没有数据流，零丢失。
        if self.plot_enabled:
            try:
                self.plot = LivePlot()
            except Exception as e:
                print(f'实时绘图不可用（{e}），继续正常采集。')
                self.plot = None
        try:
            self.ser = serial.Serial(self.device, self.baud, bytesize=8,
                                     parity='N', stopbits=1, timeout=0.05)
        except serial.SerialException as e:
            print(f'无法打开 {self.device}：{e}\n'
                  f'请确认端口没有被其他程序占用（串口监视器/MobaXterm/minicom 等）；'
                  f'Linux 下 Permission denied 见 README「Linux 串口权限」。')
            sys.exit(1)
        try:
            self.ser.set_buffer_size(rx_size=256 * 1024)
        except Exception:
            pass
        self.ser.reset_input_buffer()
        self.reader = threading.Thread(target=reader_loop,
                                       args=(self.ser, self.q, self.stop_event),
                                       daemon=True)
        self.reader.start()
        self.t_start = time.time()
        self._t_last_save = self.t_start

    def switch(self, stem, out_dir=None, label=None):
        """结束当前段并开启新文件组（毫秒级，不触碰串口）。返回上一段质量报告。

        开新组前丢弃队列中滞留的旧数据（段间到达的包不属于任何动作段）。
        """
        report = self.end_segment()
        if out_dir:
            self.writer_dir = out_dir
        self.current_stem = stem
        if label is not None:
            self.current_label = label
        # 丢弃段间滞留数据，保证新段从"现在"开始
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.writer = DatasetWriter(self.writer_dir, stem,
                                    write_flat=self.write_flat,
                                    all_ltfs=self.all_ltfs)
        self.n_packets = self.n_invalid = self.n_log = 0
        self.n_id_gaps = self.n_time_gaps = self.n_host_gaps = 0
        self._fw_ts = None
        self._host_ts = None
        self._last_id = None    # 段间 id 大跳变不计入新段质量报告
        self._intervals = []
        self._host_intervals = []
        self._time_gap_warned = False
        return report

    def end_segment(self):
        """finalize 当前文件组，返回本段质量报告（无当前段返回 None）。"""
        if self.writer is None:
            return None
        self.poll(timeout=0)            # 收尾：把队尾残留写入本段
        report = self._quality_report()
        mat_path = self.writer.finalize(save_mat=self.save_mat)
        self.last_files = {'raw': self.writer.path_raw,
                           'flat': self.writer.path_flat if self.writer.write_flat else None,
                           'npz': self.writer.path_npz,
                           'mat': mat_path,
                           'n': self.writer.n,
                           'write_flat': self.writer.write_flat}
        self.writer = None
        self.current_stem = None
        return report

    def close(self):
        """结束会话：finalize 当前组、停读线程、关串口与绘图。返回最终质量报告。"""
        report = self.end_segment()
        self.stop_event.set()
        if self.reader is not None:
            self.reader.join(timeout=2)
        if self.ser is not None:
            self.ser.close()
        if self.plot is not None:
            try:
                self.plot.close()
            except Exception:
                pass
        return report

    # -- 数据处理 ----------------------------------------------
    def poll(self, timeout=0.05):
        """取一批队列行写入当前文件组；返回本次处理的行数。"""
        batch = []
        try:
            batch.append(self.q.get(timeout=timeout))
            while True:
                batch.append(self.q.get_nowait())
        except queue.Empty:
            pass
        n = len(batch)
        if batch:
            self._process_batch(batch)
        now = time.time()
        if now - self._t_last_save >= self.save_interval:
            if self.writer is not None:
                self.writer.snapshot()
            self._t_last_save = now
        # 数据优先：解析积压时跳过本次重绘（渲染期间 GIL 被 C 端长时间持有，
        # 读线程会被饿死，内核 tty 缓冲仅 ~4KB，溢出即丢包/错行）
        if (self.plot is not None and self.writer is not None
                and self.n_packets and self.q.qsize() < 100
                and now - self._t_last_plot >= PLOT_REFRESH):
            self.plot.refresh(self.writer.rssi[-1])
            self._t_last_plot = now
        if self.q.qsize() > 500 and not self._warned_backlog:
            print('\n警告：解析速度落后于串口速率，建议关闭绘图或加大快照间隔')
            self._warned_backlog = True
        return n

    def _process_batch(self, batch):
        for raw, ts in batch:   # ts = 读线程打点的到达时刻
            text = raw.decode('ascii', errors='replace')
            if not is_csi_line(text):
                self.n_log += 1
                continue
            if self.writer is None:
                continue            # 未开段时到达的数据直接丢弃
            self.writer.write_raw_line(text)
            pkt = parse_csi_line(text)
            if pkt is None:
                self.n_invalid += 1
                continue
            # 固件时钟间隔统计（无线/发送链路的均匀性 / 空窗检测）
            fw_ts = pkt['timestamp_us']
            if self._fw_ts is not None:
                dt = (fw_ts - self._fw_ts) / 1e6
                if dt < 0:          # uint32 µs 计数器回绕（约 71.6 分钟）
                    dt += 2 ** 32 / 1e6
                self._intervals.append(dt)
                if dt > self.gap_threshold:
                    self.n_time_gaps += 1
                    if not self._time_gap_warned:
                        print(f'\n警告：本段出现 {dt:.2f}s 采集空窗'
                              f'（阈值 {self.gap_threshold}s）')
                        self._time_gap_warned = True
            self._fw_ts = fw_ts
            # 主机时钟间隔统计（串口/处理链路健康）
            if self._host_ts is not None:
                hdt = ts - self._host_ts
                self._host_intervals.append(hdt)
                if hdt > self.gap_threshold:
                    self.n_host_gaps += 1
            self._host_ts = ts
            if self._last_id is not None and pkt['id'] != self._last_id + 1:
                self.n_id_gaps += max(0, pkt['id'] - self._last_id - 1)
            self._last_id = pkt['id']
            self.writer.add(pkt, self.current_label, ts, ts - self.t_start)
            self.n_packets += 1
            self.total_packets += 1
            self.last_pkt_time = ts
            if self.plot is not None:
                self.plot.push(pkt['csi'])

    def _quality_report(self):
        """段末质量报告：双时钟采样率/间隔统计与三项达标判定。

        rate_hz 基于固件时钟（无线/发送链路），host_rate_hz 基于主机到达时刻
        （串口/处理链路）；两者对照可定位问题层。
        """
        iv = np.asarray(self._intervals, dtype=np.float64)
        span = float(iv.sum()) if iv.size else 0.0
        # rate = (N-1)/span：span 只覆盖首末包之间的间隔
        rate = ((self.n_packets - 1) / span
                if span > 0 and self.n_packets >= 2 else 0.0)
        hiv = np.asarray(self._host_intervals, dtype=np.float64)
        hspan = float(hiv.sum()) if hiv.size else 0.0
        host_rate = ((self.n_packets - 1) / hspan
                     if hspan > 0 and self.n_packets >= 2 else 0.0)
        if iv.size:
            mean_i, med_i = float(iv.mean()), float(np.median(iv))
            p95_i = float(np.percentile(iv, 95))
            max_i, std_i = float(iv.max()), float(iv.std())
        else:
            mean_i = med_i = p95_i = max_i = std_i = 0.0
        ok = self.n_packets >= 2
        return {
            'stem': self.current_stem,
            'n_packets': self.n_packets,
            'rate_hz': rate,
            'host_rate_hz': host_rate,
            'host_max_interval': float(hiv.max()) if hiv.size else 0.0,
            'n_host_gaps': self.n_host_gaps,
            'mean_interval': mean_i, 'median_interval': med_i,
            'p95_interval': p95_i, 'max_interval': max_i,
            'std_interval': std_i,
            'n_id_gaps': self.n_id_gaps,
            'n_time_gaps': self.n_time_gaps,
            'n_invalid': self.n_invalid,
            'n_log': self.n_log,
            'rate_ok': bool(ok and rate >= 0.8 * self.expected_rate),
            'gap_ok': bool(ok and max_i <= self.gap_threshold),
            'uniform_ok': bool(ok and med_i > 0 and p95_i <= 2.0 * med_i),
        }

    def qsize(self):
        return self.q.qsize()


# ---------------------------------------------------------------- 主程序

def parse_args():
    ap = argparse.ArgumentParser(
        description='实时采集 ESP32-S3 串口 CSI 数据并保存为深度学习数据集')
    ap.add_argument('--port', help='串口名（Windows: COMx；Linux: /dev/ttyACM0 等；'
                                   '默认自动选择 CH343/CH34x 口）')
    ap.add_argument('--baud', type=int, default=DEFAULT_BAUD,
                    help=f'波特率（默认 {DEFAULT_BAUD}）')
    ap.add_argument('--list-ports', action='store_true', help='列出系统串口后退出')
    ap.add_argument('--label', default=None,
                    help='本会话初始标签（默认取标签面板第一个，否则为 data）')
    ap.add_argument('--labels', default='',
                    help='标签面板，逗号分隔；按数字键 0-9 切换，如：--labels 空房间,走路,挥手')
    ap.add_argument('--out-dir', default=DEFAULT_OUT_DIR,
                    help=f'输出目录（默认 {DEFAULT_OUT_DIR}）')
    ap.add_argument('--duration', type=float, default=0,
                    help='采集时长秒（0=直到 Ctrl-C / q）')
    ap.add_argument('--save-interval', type=float, default=5.0,
                    help='npz/CSV 快照间隔秒（默认 5，越大越省 CPU）')
    ap.add_argument('--plot', action='store_true', help='实时绘图（需要 matplotlib）')
    ap.add_argument('--all-ltfs', action='store_true',
                    help='npz/mat 不做段裁剪：三段固件(len=384)保存全部三段（192 子载波）；'
                         '单段固件(len=128/256)数据本就完整，此参数无效果')
    ap.add_argument('--mat', action='store_true', help='结束时额外保存 .mat（需要 scipy）')
    ap.add_argument('--no-flat-csv', action='store_true', help='不写扁平数值 CSV')
    ap.add_argument('--probe-timeout', type=float, default=2.0,
                    help='多串口时每个口的探测时长秒（默认 2）')
    return ap.parse_args()


def main():
    args = parse_args()

    try:    # 重定向输出时防止特殊字符触发编码错误
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass

    if args.list_ports:
        ports = list_all_ports()
        ports.sort(key=lambda p: not _is_ch34x(p))  # 稳定排序：CH34x 排前
        for p in ports:
            mark = '  [CH34x]' if _is_ch34x(p) else ''
            print(f'  {p.device:10s} {p.description}{mark}')
        return

    palette = [s.strip() for s in args.labels.split(',') if s.strip()]
    if args.label is None:
        label = palette[0] if palette else 'data'
    else:
        label = args.label
    if label not in palette:
        palette.insert(0, label)
    read_key = make_key_reader()
    print('标签面板: ' + ', '.join(f'{i}={s}' for i, s in enumerate(palette))
          + ('（按数字键切换）' if read_key else ''))

    collector = CsiCollector(device=args.port, baud=args.baud,
                             probe_timeout=args.probe_timeout,
                             all_ltfs=args.all_ltfs, save_mat=args.mat,
                             save_interval=args.save_interval, plot=args.plot,
                             write_flat=not args.no_flat_csv)
    collector.open()
    device = collector.device

    stem = f'csi_{time.strftime("%Y%m%d_%H%M%S")}_{label}'
    mode = ('全部数据（不裁剪）' if args.all_ltfs
            else '自适应（三段固件仅存 HT-LTF 段；单段固件为完整数据）')
    print(f'npz 保存内容: {mode}')
    collector.switch(stem, out_dir=args.out_dir, label=label)
    writer = collector.writer
    print('输出文件:')
    print(f'  {writer.path_raw}')
    if writer.write_flat:
        print(f'  {writer.path_flat}')
    print(f'  {writer.path_npz}')

    print(f'开始采集（{device} @ {args.baud}，8N1）。'
          + ('按 q 或 Ctrl-C 结束。' if read_key else '按 Ctrl-C 结束。'))

    t_start = collector.t_start
    t_last_status = time.time()
    pkts_prev = 0
    stop_event = collector.stop_event

    def print_status(pkt_rate):
        line = (f'\r{device} | 包数 {collector.n_packets:6d} | {pkt_rate:6.1f} 包/s | '
                f'无效 {collector.n_invalid} | 日志行 {collector.n_log} | 标签 {label}')
        if collector.qsize() > 100:
            line += f' | 处理队列积压 {collector.qsize()}'
        print(line.ljust(110), end='', flush=True)

    try:
        while not stop_event.is_set():
            collector.poll(timeout=0.05)
            t_now = time.time()
            if t_now - t_last_status >= 1.0:
                dt = max(t_now - t_last_status, 1e-6)
                # 只统计有效 CSI 包（日志行不计入采样率）
                print_status((collector.n_packets - pkts_prev) / dt)
                pkts_prev = collector.n_packets
                t_last_status = t_now

            if read_key:
                ch = read_key()
                if ch in ('q', 'Q'):
                    stop_event.set()
                elif ch and ch.isdigit() and int(ch) < len(palette):
                    label = palette[int(ch)]
                    collector.current_label = label

            if args.duration and (time.time() - t_start) >= args.duration:
                stop_event.set()
    except KeyboardInterrupt:
        pass
    finally:
        collector.close()
        files = collector.last_files or {}
        print('\n' + '=' * 60)
        print(f'采集完成：有效包 {files.get("n", 0)}，无效行 {collector.n_invalid}，'
              f'日志行 {collector.n_log}，用时 {time.time() - t_start:.1f}s')
        if files:
            print(f'  {files["raw"]}')
            if files.get('flat'):
                print(f'  {files["flat"]}')
            print(f'  {files["npz"]}')
            if files.get('mat'):
                print(f'  {files["mat"]}')


if __name__ == '__main__':
    main()
