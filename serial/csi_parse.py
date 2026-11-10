# -*- coding: utf-8 -*-
"""esp-csi 串口 CSV 数据解析库。

解析 ESP32 通过串口输出的 CSI_DATA 行（esp-csi 标准 CSV 格式，25 列），
还原为复数子载波数组和元数据。被 csi_logger.py（实时采集）和
csv_to_npz.py（离线转换）共用。

data 列为 384 个整数（ESP32-S3, HT40）：每 2 个表示 1 个复数子载波，
偶数下标为虚部 Q、奇数下标为实部 I（与 esp-csi README 一致：先虚后实），
共 192 个复数 = LLTF 64 + HT-LTF 64 + STBC-HT-LTF 64。

解析逻辑参考 examples/get-started/tools/csi_data_read_parse.py。
"""

import csv
import json

import numpy as np

# CSI_DATA 行的 25 列（与固件 app_main.c 打印的表头一致）
CSI_COLUMNS = [
    'type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth',
    'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding', 'sgi',
    'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel',
    'local_timestamp', 'ant', 'sig_len', 'rx_format', 'len', 'first_word',
    'data',
]

HEADER_LINE = ','.join(CSI_COLUMNS)

# len=384 时的三段 LTF 子载波布局（下标为复数子载波序号）
LTF_SEGMENTS = (
    (0, 64, 'LLTF'),
    (64, 128, 'HT-LTF'),
    (128, 192, 'STBC-HT-LTF'),
)

CSI_DATA_PREFIX = 'CSI_DATA'


def is_csi_line(text):
    """判断一行文本是否为 CSI_DATA 数据行。"""
    return text.strip().startswith(CSI_DATA_PREFIX)


def parse_csi_line(text):
    """解析一行 CSI_DATA 文本。

    返回 dict：
      id            int   包序号（发送端 ESP-NOW payload 计数）
      mac           str   对端 MAC
      rssi          int   接收信号强度 dBm
      sig_mode      int   0=11b, 1=11g/n(HT)
      mcs           int   MCS 序号
      bandwidth     int   0=20MHz, 1=40MHz
      noise_floor   int   底噪 dBm
      channel       int   主信道
      sec_channel   int   副信道 (1=above, 2=below, 0=none)
      timestamp_us  int   local_timestamp，微秒
      sig_len       int   帧长字节
      rx_format     int   固件打印的 rx_format 列（S3 上实际为 sig_mode）
      first_word    int   first_word_invalid 标志
      iq            np.ndarray  原始 Q/I 交替数组（长度=len，int16 存储）
      csi           np.ndarray  complex64 数组，长度 len//2
    解析失败（列数不符 / JSON 错误 / 长度不匹配）返回 None。
    """
    text = text.strip()
    if not text.startswith(CSI_DATA_PREFIX):
        return None
    try:
        row = next(csv.reader([text]))
    except csv.Error:
        return None
    if len(row) != len(CSI_COLUMNS):
        return None
    try:
        raw = json.loads(row[24])
        pkt_len = int(row[22])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, list) or pkt_len <= 0:
        return None
    if len(raw) != pkt_len or pkt_len % 2 != 0:
        return None

    try:
        # 固件打印的是 (int16_t)(compensate_gain * buf[i])，增益补偿后可能
        # 超出 int8 范围，故用 int16 存储
        iq = np.asarray(raw, dtype=np.int16)
        # 偶数下标 = 虚部 Q，奇数下标 = 实部 I
        csi = (iq[1::2].astype(np.float32)
               + 1j * iq[0::2].astype(np.float32)).astype(np.complex64)
    except (ValueError, TypeError, OverflowError):
        return None

    def _int(index):
        try:
            return int(row[index])
        except ValueError:
            return 0

    return {
        'id': _int(1),
        'mac': row[2],
        'rssi': _int(3),
        'sig_mode': _int(5),
        'mcs': _int(6),
        'bandwidth': _int(7),
        'noise_floor': _int(14),
        'channel': _int(16),
        'sec_channel': _int(17),
        'timestamp_us': _int(18),
        'sig_len': _int(20),
        'rx_format': _int(21),
        'first_word': _int(23),
        'iq': iq,
        'csi': csi,
    }
