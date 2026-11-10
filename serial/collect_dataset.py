# -*- coding: utf-8 -*-
"""collect_dataset.py — 暴力行为检测 CSI 数据集采集驱动（ESP32 版）。

复刻 collect_dataset.sh（Intel CSITool 版）的核心采集流程与标注规范：
  * session 驱动动作序列，每个动作录制一组独立数据文件
    （csi_<时间戳>_<session>_<序号>_<动作名>.npz/_raw.csv/_data.csv）
  * 每 run 一个标注日志（保持原脚本格式），通过 dat_file 字段与数据组关联
  * 大字提示动作 → 开录 → 逐秒倒计时（末 5 秒大字）→ 结束 → 休息 1s
  * 录制健康检查：采集空窗 / 串口链路超时实时告警；
    段末输出质量报告（采样率/间隔均匀性/断号）并写入 quality_report.csv

与 CSITool 版的差异（刻意为之）：
  * 不需要 WiFi 连接与 hping3：发送板 ESP-NOW 以固定速率发包，接收板串口输出
  * log_to_file → csi_logger.CsiCollector：串口整个进程只打开一次，
    动作切换是进程内毫秒级换文件组，屏幕计时与实际采集起点同步

用法（serial/ 目录，uv 环境）：
  uv run python collect_dataset.py session_B
  uv run python collect_dataset.py -r 3 session_B
  uv run python collect_dataset.py session_D 3
可选：--port COMx --plot --prompt-time 1 --expected-rate 100 --gap-threshold 0.5
      --dat-dir csi_actions --log-dir csi_log --all-ltfs --mat

采集建议：每天先跑 session_A（空房间基线，串口首开的增益基线由它覆盖）。
"""

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi_logger import DEFAULT_BAUD, CsiCollector

_SLEEP_STEP = 1.0   # 倒计时步长秒（仅供测试调快；正常使用勿改）

# ============================================================
# Session 动作表（与 collect_dataset.sh 一一对应）
#   type=action: name 动作名 / duration 秒 / people 人数 / label 暴力标签(None=无)
#   type=people: duration 秒（人数检测，人数取命令行参数）
# ============================================================
SESSIONS = {
    'session_A': [  # 0 人基线：13 段各 3 秒
        dict(type='action', name='emptyroom', duration=3, people=0, label=None),
    ] * 13,
    'session_B': [  # 1 人 HAR + 暴力模拟
        dict(type='action', name='standstill', duration=3, people=1, label=0),
        dict(type='action', name='walk',       duration=3, people=1, label=0),
        dict(type='action', name='sit',        duration=3, people=1, label=0),
        dict(type='action', name='standup',    duration=3, people=1, label=0),
        dict(type='action', name='wave',       duration=3, people=1, label=0),
        dict(type='action', name='bend',       duration=3, people=1, label=0),
        dict(type='action', name='jump',       duration=3, people=1, label=0),
        dict(type='action', name='kicksim1',   duration=3, people=1, label=1),
        dict(type='action', name='kicksim2',   duration=3, people=1, label=1),
        dict(type='action', name='kicksim3',   duration=3, people=1, label=1),
        dict(type='action', name='punchsim1',  duration=3, people=1, label=1),
        dict(type='action', name='punchsim2',  duration=3, people=1, label=1),
        dict(type='action', name='punchsim3',  duration=3, people=1, label=1),
    ],
    'session_C': [  # 2 人交互（含暴力/非暴力）
        dict(type='action', name='approach',   duration=3, people=2, label=0),
        dict(type='action', name='leave',      duration=3, people=2, label=0),
        dict(type='action', name='handshake',  duration=3, people=2, label=0),
        dict(type='action', name='highfive',   duration=3, people=2, label=0),
        dict(type='action', name='pointleft',  duration=3, people=2, label=0),
        dict(type='action', name='pointright', duration=3, people=2, label=0),
        dict(type='action', name='push',       duration=3, people=2, label=1),
        dict(type='action', name='kickleft',   duration=3, people=2, label=1),
        dict(type='action', name='kickright',  duration=3, people=2, label=1),
        dict(type='action', name='punchleft',  duration=3, people=2, label=1),
        dict(type='action', name='punchright', duration=3, people=2, label=1),
    ],
    'session_D': [  # 人数检测：1 段 30 秒，人数取命令行参数
        dict(type='people', duration=30),
    ],
}

QUALITY_COLUMNS = ['run', 'session', 'seq', 'type', 'name', 'people', 'label',
                   'dat_file', 'n_packets', 'rate_hz', 'host_rate_hz',
                   'host_max_interval_ms', 'mean_interval_ms',
                   'median_interval_ms', 'p95_interval_ms', 'max_interval_ms',
                   'std_interval_ms', 'id_gaps', 'time_gaps', 'host_gaps',
                   'invalid_lines', 'rate_ok', 'gap_ok', 'uniform_ok']


def _fmt_t(t):
    """unix 时间戳，保留 3 位小数（对应原脚本 date +%s.%N）。"""
    return f'{t:.3f}'


def _anno(anno_f, line):
    anno_f.write(line + '\n')
    anno_f.flush()


try:
    import pyfiglet
except ImportError:
    pyfiglet = None

# 候选粗体字体（首个可用者优先；均为大笔画字体，字母自带分隔）
_FIGLET_FONTS = ('ansi_regular', 'ansi_shadow', 'colossal', 'block', 'doom', 'big')


def figlet_text(text, scale=1):
    """渲染 ASCII 大字（pyfiglet 粗体字体）；失败时返回 None（调用方回退 ## 框）。

    含非 ASCII 字符（如中文）或未安装 pyfiglet 时返回 None。
    scale=2 时横向纵向再各放大一倍（适合倒计时数字）。
    """
    if pyfiglet is None or not text.isascii():
        return None
    art = None
    for font in _FIGLET_FONTS:
        try:
            art = pyfiglet.figlet_format(text, font=font, width=300)
        except Exception:
            art = None
        if art and art.strip():
            break
        art = None
    if not art:
        return None
    lines = art.rstrip('\n').splitlines()
    while lines and not lines[-1].strip():   # 字体输出末尾常见的全空行
        lines.pop()
    if scale == 2:
        lines = [''.join(c * 2 for c in ln) for ln in lines]
        doubled = []
        for ln in lines:
            doubled += [ln, ln]
        lines = doubled
    return '\n'.join(lines)


def big_banner(text, scale=1):
    """大号显示：ASCII 用 figlet 字体，含中文时退回 ## 框。"""
    art = figlet_text(text, scale=scale)
    if art is None:
        line = '#' * (len(text) * 2 + 6)
        print(line)
        print(f'##  {text}  ##')
        print(line)
    else:
        print(art)


def show_prompt(text, duration, people):
    """大字提示下一个动作（清屏 + 框 + 横幅），停留 --prompt-time 秒。"""
    print('\033[2J\033[H', end='')     # 清屏
    print('╔══════════════════════════════════════════════════════╗')
    print('║                                                      ║')
    print(f'║   下一个动作：{text}')
    print(f'║   持续时间：{duration} 秒')
    print(f'║   当前人数：{people}')
    print('║                                                      ║')
    print('╚══════════════════════════════════════════════════════╝')
    print()
    big_banner(text)
    print()


def countdown(collector, duration, title, banner=None):
    """两排大字倒计时：第一排动作名持续显示，第二排剩余秒数（大字）。

    轮询采集器并做实时健康检查。
    """
    banner_text = banner if banner is not None else title
    end = time.time() + duration
    last_shown = None
    warned_link = False
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        collector.poll(timeout=0.02)
        # 串口链路检查：超过 3 秒没有任何有效包
        if (collector.last_pkt_time is not None
                and time.time() - collector.last_pkt_time > 3.0
                and not warned_link):
            print(f'\n警告：串口已 3 秒无数据，请检查发送板与串口连接！')
            warned_link = True
        rem = max(1, math.ceil(remaining))
        if rem != last_shown:
            last_shown = rem
            print('\033[2J\033[H', end='')
            print(f'===== [RECORDING] {title} — 剩余 {rem}s =====')
            print()
            # 第一排：动作名称（每秒重绘同内容，视觉上持续不消失）
            big_banner(banner_text)
            print()
            # 第二排：倒计时大字
            big_banner(str(rem), scale=2)
        # 高频轮询（一轮 ≤40ms）：数据随到随处理，host_time 连续、
        # 串口驱动缓冲不积压；显示重绘只在整秒变化时进行，与采集解耦。
        time.sleep(0.02)
    print()


def print_report(report):
    """打印段末质量报告（采样率 / 间隔均匀性 / 断号）。"""
    if report is None:
        return
    marks = ('✓' if report['rate_ok'] else '⚠',
             '✓' if report['gap_ok'] else '⚠',
             '✓' if report['uniform_ok'] else '⚠')
    print(f'  [质量] {report["stem"]}')
    print(f'    包数 {report["n_packets"]}，固件采样率 {report["rate_hz"]:.1f} Hz'
          f'（无线/发送链路），主机到达率 {report["host_rate_hz"]:.1f} Hz'
          f'（串口/处理链路）')
    print(f'    固件间隔 ms: 均值 {report["mean_interval"]*1e3:.1f} / '
          f'中位 {report["median_interval"]*1e3:.1f} / '
          f'P95 {report["p95_interval"]*1e3:.1f} / '
          f'最大 {report["max_interval"]*1e3:.1f} / '
          f'标准差 {report["std_interval"]*1e3:.1f}')
    print(f'    主机最大到达间隔 {report["host_max_interval"]*1e3:.1f} ms，'
          f'主机空窗 {report["n_host_gaps"]}')
    print(f'    id断号 {report["n_id_gaps"]}，空窗 {report["n_time_gaps"]}，'
          f'无效行 {report["n_invalid"]} | '
          f'采样率 {marks[0]} 空窗 {marks[1]} 均匀性 {marks[2]}')
    # 双时钟对照诊断：主机无丢包而固件速率不达标 → 串口打印带宽上限
    if (not report['rate_ok'] and report['n_host_gaps'] == 0
            and report['host_rate_hz'] >= report['rate_hz'] * 0.95):
        print('    提示：主机链路无丢包，丢包发生在固件侧——当前波特率下每行 CSI '
              '打印耗时约 14ms，是采样率上限。'
              '把控制台波特率提到 2000000（menuconfig 重新烧录接收板，'
              '采集时加 --baud 2000000）可回到 ~100Hz。')


def append_quality_row(csv_path, run, session, seq, blk_type, name, people,
                       label, stem, report):
    """追加一行段末质量报告到 quality_report.csv（机器可读，便于剔除劣质段）。"""
    new_file = not os.path.exists(csv_path)
    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
        if new_file:
            f.write(','.join(QUALITY_COLUMNS) + '\n')
        r = report or {}
        row = [run, session, seq, blk_type, name, people,
               '' if label is None else label, stem,
               r.get('n_packets', 0), f'{r.get("rate_hz", 0):.2f}',
               f'{r.get("host_rate_hz", 0):.2f}',
               f'{r.get("host_max_interval", 0)*1e3:.2f}',
               f'{r.get("mean_interval", 0)*1e3:.2f}',
               f'{r.get("median_interval", 0)*1e3:.2f}',
               f'{r.get("p95_interval", 0)*1e3:.2f}',
               f'{r.get("max_interval", 0)*1e3:.2f}',
               f'{r.get("std_interval", 0)*1e3:.2f}',
               r.get('n_id_gaps', 0), r.get('n_time_gaps', 0),
               r.get('n_host_gaps', 0), r.get('n_invalid', 0),
               int(r.get('rate_ok', False)), int(r.get('gap_ok', False)),
               int(r.get('uniform_ok', False))]
        f.write(','.join(str(v) for v in row) + '\n')


def run_action_block(collector, anno, args, run, seq, blk, quality_csv):
    """录制一个动作块：提示 → 开录 → 倒计时 → 结束 → 质量报告。"""
    name = blk['name']
    duration = blk['duration']
    people = blk['people']
    label = blk.get('label')

    stem_ts = time.strftime('%Y%m%d_%H%M%S')
    stem = f'csi_{stem_ts}_{args.session}_{seq:05d}_{name}'

    show_prompt(name, duration, people)
    time.sleep(max(0.0, args.prompt_time))

    print(f'===== [START] {name} ({duration}s) =====')
    t_start = time.time()
    collector.switch(stem, out_dir=args.dat_dir, label=name)
    line = f'action_start, {_fmt_t(t_start)}, {name}, people={people}'
    if label is not None:
        line += f', label={label}'
    line += f', dat_file={stem}'
    _anno(anno, line)

    countdown(collector, duration, name, banner=name)

    t_end = time.time()
    line = f'action_end, {_fmt_t(t_end)}, {name}, people={people}'
    if label is not None:
        line += f', label={label}'
    line += f', dat_file={stem}'
    _anno(anno, line)

    report = collector.end_segment()
    print_report(report)
    append_quality_row(quality_csv, run, args.session, seq, 'action', name,
                       people, label, stem, report)

    time.sleep(1)   # buffer 休息时间（与原脚本一致）
    return seq + 1


def run_people_block(collector, anno, args, run, seq, blk, quality_csv):
    """录制人数检测块（people_change_* 标注）。"""
    count = args.people_count
    duration = blk['duration']
    stem_ts = time.strftime('%Y%m%d_%H%M%S')
    stem = f'csi_{stem_ts}_{args.session}_{seq:05d}_peoplecount'

    show_prompt(f'人数 → {count}人', duration, count)
    time.sleep(max(0.0, args.prompt_time))

    print(f'===== [START] 人数={count}人 ({duration}s) =====')
    t_start = time.time()
    collector.switch(stem, out_dir=args.dat_dir, label=f'people_{count}')
    _anno(anno, f'people_change_start, {_fmt_t(t_start)}, people={count}, '
                f'dat_file={stem}')

    countdown(collector, duration, f'人数={count}人', banner=f'{count}PEOPLE')

    t_end = time.time()
    _anno(anno, f'people_change_end, {_fmt_t(t_end)}, people={count}, '
                f'dat_file={stem}')

    report = collector.end_segment()
    print_report(report)
    append_quality_row(quality_csv, run, args.session, seq, 'people',
                       f'people_{count}', count, None, stem, report)

    time.sleep(1)
    return seq + 1


def parse_args():
    ap = argparse.ArgumentParser(
        description='暴力行为检测 CSI 数据集采集驱动（ESP32 版，'
                    '复刻 collect_dataset.sh 流程与标注规范）')
    ap.add_argument('session', choices=sorted(SESSIONS),
                    help='session_A 空房间基线 | session_B 单人HAR+暴力模拟 | '
                         'session_C 双人交互 | session_D 人数检测')
    ap.add_argument('people_count', nargs='?', type=int, default=1,
                    help='session_D 的人数（1-10），其他 session 忽略')
    ap.add_argument('-r', '--repeat', type=int, default=1,
                    help='重复采集次数（默认 1）')
    ap.add_argument('--port', help='串口名（默认自动选择 CH343 口，全程只开一次）')
    ap.add_argument('--baud', type=int, default=DEFAULT_BAUD,
                    help=f'波特率（默认 {DEFAULT_BAUD}）')
    ap.add_argument('--probe-timeout', type=float, default=2.0,
                    help='多串口候选时每个口的探测时长秒（默认 2）')
    ap.add_argument('--plot', action='store_true',
                    help='实时绘图（同 csi_logger --plot：HT-LTF 瀑布图 + '
                         '当前包子载波幅度曲线，需要 GUI 后端）')
    ap.add_argument('--prompt-time', type=float, default=1.0,
                    help='每动作前大字提示停留秒数（默认 1，与参考脚本一致）')
    ap.add_argument('--expected-rate', type=float, default=100.0,
                    help='名义采样率 Hz（默认 100，即发送端 CONFIG_SEND_FREQUENCY）；'
                         '质量报告按其 80%% 判定采样率达标')
    ap.add_argument('--gap-threshold', type=float, default=0.5,
                    help='采集空窗判定阈值秒（默认 0.5）')
    ap.add_argument('--dat-dir', default='csi_actions',
                    help='数据文件输出目录（默认 csi_actions）')
    ap.add_argument('--log-dir', default='csi_log',
                    help='标注日志与质量报告目录（默认 csi_log）')
    ap.add_argument('--all-ltfs', action='store_true',
                    help='npz 不做段裁剪：三段固件(len=384)保存全部三段；'
                         '单段固件(len=128/256)数据本就完整，此参数无效果')
    ap.add_argument('--mat', action='store_true', help='每动作额外保存 .mat')
    return ap.parse_args()


def main():
    args = parse_args()

    try:    # 重定向输出时防止特殊字符（块状大字）触发编码错误
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass

    if args.session == 'session_D' and not (1 <= args.people_count <= 10):
        print('人数检测需要指定人数 (1-10)：'
              'uv run python collect_dataset.py session_D 3')
        sys.exit(1)
    if args.repeat < 1:
        print('重复次数必须 >= 1')
        sys.exit(1)

    os.makedirs(args.dat_dir, exist_ok=True)
    os.makedirs(args.log_dir, exist_ok=True)
    quality_csv = os.path.join(args.log_dir, 'quality_report.csv')

    print()
    print('=' * 56)
    print(' CSI 采集一体化脚本（ESP32 版）')
    print(f' Session:    {args.session}'
          + (f' ({args.people_count}人)' if args.session == 'session_D' else ''))
    print(f' 重复:       {args.repeat} 次')
    print(f' 名义采样率: {args.expected_rate:.0f} Hz')
    print(f' 数据目录:   {os.path.abspath(args.dat_dir)}')
    print(f' 标注目录:   {os.path.abspath(args.log_dir)}')
    print(f' 时间:       {time.strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 56)
    print()

    collector = CsiCollector(device=args.port, baud=args.baud,
                             probe_timeout=args.probe_timeout,
                             all_ltfs=args.all_ltfs, save_mat=args.mat,
                             gap_threshold=args.gap_threshold,
                             expected_rate=args.expected_rate,
                             plot=args.plot)
    print('打开串口（整个采集过程只开一次，请耐心等待首包稳定）...')
    collector.open()
    print(f'串口就绪: {collector.device} @ {args.baud}\n')

    anno_path = None
    anno_done = False
    interrupted = False
    try:
        for run in range(1, args.repeat + 1):
            run_ts = time.strftime('%Y%m%d_%H%M%S')
            anno_name = f'csi_{run_ts}_{args.session}_{run:05d}_annotation.log'
            anno_path = os.path.join(args.log_dir, anno_name)
            anno = open(anno_path, 'w', encoding='utf-8', newline='\n')
            anno_done = False
            _anno(anno, f'session_start, {_fmt_t(time.time())}, {args.session}')

            print(f'===== 第 {run}/{args.repeat} 次采集 =====')
            print(f'  标注文件: {anno_path}\n')

            seq = 1
            for blk in SESSIONS[args.session]:
                if blk['type'] == 'action':
                    seq = run_action_block(collector, anno, args, run, seq,
                                           blk, quality_csv)
                else:
                    seq = run_people_block(collector, anno, args, run, seq,
                                           blk, quality_csv)

            _anno(anno, f'session_end, {_fmt_t(time.time())}, {args.session}')
            anno.close()
            anno_done = True
            print(f'\n===== 第 {run}/{args.repeat} 次采集完成 =====')
            print(f'  标注文件: {anno_path}')

            if run < args.repeat:
                print('\n准备下一次采集...（如需更换受试者，现在操作）')
                time.sleep(3)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        report = collector.close()
        if report is not None:
            print_report(report)
            run_no = -1
            append_quality_row(quality_csv, run_no, args.session, -1,
                               'unterminated', collector.current_stem or '',
                               None, None, collector.current_stem or '',
                               report)
        if interrupted and anno_path is not None and not anno_done:
            # 中断时补写 session_end（保持原格式），控制台另行说明
            with open(anno_path, 'a', encoding='utf-8', newline='\n') as anno:
                _anno(anno, f'session_end, {_fmt_t(time.time())}, {args.session}')
            print('\n[中断] 本次 run 提前结束，数据文件与标注已收尾保存。')
        print(f'\n质量报告: {quality_csv}')
        if interrupted:
            sys.exit(130)


if __name__ == '__main__':
    main()
