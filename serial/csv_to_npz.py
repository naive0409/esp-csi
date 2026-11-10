# -*- coding: utf-8 -*-
"""csv_to_npz.py — 把已抓取的串口文本/CSV 转成深度学习数据集格式。

输入可以是：
  * csi_logger.py 生成的 *_raw.csv
  * MobaXterm 等串口工具导出的文本（自动跳过日志行，只取 CSI_DATA 行）
  * 固件直接输出的 CSV（带或不带表头均可）

输出（与 csi_logger.py 相同的数据集格式）：
  <输入名>_data.npz  numpy 存档（csi/amp/phase/iq + 元数据数组）
                     默认只保存 HT-LTF 段（64 子载波）；--all-ltfs 保存全部三段
  <输入名>_data.csv  扁平数值表（MATLAB readmatrix / pandas 直接可读，恒为全部数据）

用法：
  python csv_to_npz.py MobaXterm_SerialCOM_20261103_100527.txt
  python csv_to_npz.py a_raw.csv b_raw.csv --out-dir csi_sessions --label 走路
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi_parse import parse_csi_line


def convert_file(path, out_dir, label, all_ltfs=False):
    print(f'读取 {path} ...')
    pkts = []
    n_skip = 0
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('CSI_DATA'):
                n_skip += 1
                continue
            pkt = parse_csi_line(line)
            if pkt is None:
                n_skip += 1
                continue
            pkts.append(pkt)
    if not pkts:
        print(f'  未解析到有效 CSI_DATA 行（跳过 {n_skip} 行）')
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)

    csi_full = np.stack([p['csi'] for p in pkts])
    iq_full = np.stack([p['iq'] for p in pkts])
    width = csi_full.shape[1]
    if width == 192 and not all_ltfs:
        # 三段布局（len=384）：默认只保留 HT-LTF 段（训练推荐）
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
    data = {
        'csi': csi,
        'amp': np.abs(csi).astype(np.float32),
        'phase': np.angle(csi).astype(np.float32),
        'iq': iq,
        'id': np.asarray([p['id'] for p in pkts], np.int32),
        'rssi': np.asarray([p['rssi'] for p in pkts], np.int16),
        'channel': np.asarray([p['channel'] for p in pkts], np.int16),
        'sec_channel': np.asarray([p['sec_channel'] for p in pkts], np.int16),
        'local_timestamp_us': np.asarray([p['timestamp_us'] for p in pkts], np.int64),
        'sig_len': np.asarray([p['sig_len'] for p in pkts], np.int16),
        'first_word': np.asarray([p['first_word'] for p in pkts], np.int8),
        'label': np.asarray([label] * len(pkts)),
        'source_file': os.path.basename(path),
        'mac': pkts[0]['mac'],
        'ltf_segments': seg_note,
        'iq_layout': 'iq 偶数下标=虚部Q, 奇数下标=实部I (先虚后实)',
    }

    path_npz = os.path.join(out_dir, stem + '_data.npz')
    np.savez_compressed(path_npz, **data)

    # 扁平数值 CSV（恒为全部数据，与 csi_logger 生成的 *_data.csv 格式一致）
    flat_cols = (['seq', 'id', 'rssi', 'channel', 'sec_channel',
                  'local_timestamp_us', 'sig_len', 'first_word', 'label']
                 + [f'iq_{i:04d}' for i in range(iq_full.shape[1])])
    path_flat = os.path.join(out_dir, stem + '_data.csv')
    with open(path_flat, 'w', encoding='utf-8', newline='\n') as f:
        f.write(','.join(flat_cols) + '\n')
        for i, p in enumerate(pkts):
            meta = (f'{i},{p["id"]},{p["rssi"]},{p["channel"]},'
                    f'{p["sec_channel"]},{p["timestamp_us"]},{p["sig_len"]},'
                    f'{p["first_word"]},{label}')
            f.write(meta + ',' + ','.join(map(str, p['iq'].tolist())) + '\n')

    mode = '完整数据' if (all_ltfs or width != 192) else 'HT-LTF段(64复数)'
    print(f'  有效包 {len(pkts)}（跳过 {n_skip} 行），csi 形状 {csi.shape}，npz 内容: {mode}')
    print(f'  -> {path_npz}')
    print(f'  -> {path_flat}')
    return path_npz


def main():
    ap = argparse.ArgumentParser(description='把串口抓取文本/原始 CSV 转成 npz + 扁平 CSV 数据集')
    ap.add_argument('inputs', nargs='+', help='输入文件（raw.csv / MobaXterm 导出文本）')
    ap.add_argument('--out-dir', default=None,
                    help='输出目录（默认与各输入文件相同）')
    ap.add_argument('--label', default='data', help='给本批数据打的标签')
    ap.add_argument('--all-ltfs', action='store_true',
                    help='npz 保存全部三段 LTF（192 子载波）；默认只保存 HT-LTF 段（64 子载波，训练推荐）')
    args = ap.parse_args()

    for path in args.inputs:
        out_dir = args.out_dir or os.path.dirname(os.path.abspath(path))
        convert_file(path, out_dir, args.label, all_ltfs=args.all_ltfs)


if __name__ == '__main__':
    main()
