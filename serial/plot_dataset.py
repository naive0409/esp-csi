# -*- coding: utf-8 -*-
"""plot_dataset.py — 可视化已保存的 CSI 数据集（npz / 扁平 CSV）。

同时兼作「如何在 Python 里加载数据集」的使用示例。

用法：
  python plot_dataset.py csi_sessions/csi_20261103_120000_data.npz
  python plot_dataset.py csi_sessions/csi_20261103_120000_data.csv
  python plot_dataset.py xxx.npz --rows 500 --subcarrier 10 --save out.png
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi_parse import LTF_SEGMENTS  # noqa: E402


def load_dataset(path):
    """加载数据集，返回 (amp, phase, t, labels, seg_desc)。支持 npz 与扁平 CSV。"""
    if path.endswith('.npz'):
        d = np.load(path, allow_pickle=False)
        amp, phase = d['amp'], d['phase']
        t = d['t_rel'] if 't_rel' in d else (
            d['local_timestamp_us'] / 1e6 if 'local_timestamp_us' in d
            else np.arange(amp.shape[0]))
        labels = d['label'] if 'label' in d else None
        seg_desc = str(d['ltf_segments']) if 'ltf_segments' in d else ''
        return amp, phase, t, labels, seg_desc
    if path.endswith('.csv'):
        # 扁平 CSV：元数据列 + iq_XXXX 列（偶=Q 虚部，奇=I 实部）
        m = np.genfromtxt(path, delimiter=',', names=True,
                          dtype=None, encoding='utf-8')
        iq_names = [n for n in m.dtype.names if n.startswith('iq_')]
        iq = np.vstack([m[n] for n in iq_names]).T.astype(np.int16)
        csi = (iq[:, 1::2].astype(np.float32)
               + 1j * iq[:, 0::2].astype(np.float32)).astype(np.complex64)
        amp = np.abs(csi)
        phase = np.angle(csi)
        ts = (m['local_timestamp_us'] if 'local_timestamp_us' in m.dtype.names
              else np.arange(amp.shape[0]))
        t = np.asarray(ts, np.float64) / 1e6
        labels = m['label'] if 'label' in m.dtype.names else None
        return amp, phase, t, labels, ''
    raise ValueError(f'不支持的文件类型: {path}')


def main():
    ap = argparse.ArgumentParser(description='可视化 CSI 数据集（npz/扁平 CSV）')
    ap.add_argument('file', help='数据集文件 *_data.npz 或 *_data.csv')
    ap.add_argument('--rows', type=int, default=0, help='只画前 N 行（0=全部）')
    ap.add_argument('--subcarrier', type=int, default=10, help='时序图子载波序号')
    ap.add_argument('--save', default='', help='保存图片路径（不指定则弹窗显示）')
    args = ap.parse_args()

    import matplotlib
    if args.save:
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    amp, phase, t, labels, seg_desc = load_dataset(args.file)
    if args.rows:
        amp, phase, t = amp[:args.rows], phase[:args.rows], t[:args.rows]
    print(f'数据形状: amp={amp.shape}, 时长={t[-1] - t[0]:.2f}s'
          + (f', 标签: {sorted(set(labels.tolist()))}' if labels is not None else '')
          + (f'\n子载波说明: {seg_desc}' if seg_desc else ''))

    fig, axes = plt.subplots(3, 1, figsize=(10, 10), constrained_layout=True)

    im = axes[0].imshow(amp.T, aspect='auto', interpolation='nearest', origin='lower',
                        extent=[t[0], t[-1] if len(t) > 1 else t[0] + 1, 0, amp.shape[1]])
    if amp.shape[1] == 192:
        # 完整三段布局：画出段边界并标注
        axes[0].set_title('|CSI| amplitude heatmap (all subcarriers)')
        for a, b, name in LTF_SEGMENTS:
            axes[0].axhline(b, color='w', lw=0.6, ls='--', alpha=0.6)
            axes[0].text(0.99, (a + b) / 2, name, color='w', fontsize=8,
                         ha='right', va='center',
                         transform=axes[0].get_yaxis_transform())
    else:
        # 单段数据集（如默认仅 HT-LTF 段）
        axes[0].set_title('|CSI| amplitude heatmap'
                          + (f' — {seg_desc}' if seg_desc else ''))
    axes[0].set_xlabel('time (s)')
    axes[0].set_ylabel('subcarrier index')
    fig.colorbar(im, ax=axes[0], label='|CSI|')

    sc = args.subcarrier
    for a, b, name in LTF_SEGMENTS:
        if a <= sc < b:
            axes[1].axhline(np.median(amp[:, sc]), color='gray', lw=0.5, ls=':')
    axes[1].plot(t - t[0], amp[:, sc], lw=0.8)
    axes[1].set_title(f'amplitude of subcarrier {sc} vs time')
    axes[1].set_xlabel('time (s)')
    axes[1].set_ylabel('|CSI|')

    axes[2].plot(t - t[0], phase[:, sc], lw=0.8)
    axes[2].set_title(f'phase of subcarrier {sc} vs time')
    axes[2].set_xlabel('time (s)')
    axes[2].set_ylabel('phase (rad)')

    if args.save:
        fig.savefig(args.save, dpi=150)
        print(f'图片已保存: {args.save}')
    else:
        plt.show()


if __name__ == '__main__':
    main()
