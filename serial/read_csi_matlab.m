%% read_csi_matlab.m — 在 MATLAB 中加载 CSI 数据集的示例
%% 数据来源: csi_logger.py / csv_to_npz.py 生成的数据集文件
%%
%% 提供两种方式:
%%   方式一: 读扁平数值 CSV  (*_data.csv)   — 无需任何工具箱, readmatrix 直接读
%%   方式二: 读 mat 文件     (*_data.mat)   — csi_logger.py --mat 生成, load 直接读
%% MATLAB 路径提示: 将下面文件路径改成自己的数据集路径。

clear; clc;

%% ========== 方式一: 扁平数值 CSV ==========
% 每行 = 一个 CSI 包; 前 12 列为元数据:
%   seq, host_time, t_rel, label(文本列, readmatrix 会变 NaN), id, rssi,
%   channel, sec_channel, local_timestamp_us, sig_len, len, first_word
% 之后 384 列为 iq_0000..iq_0383 (int8, 偶数列=虚部Q, 奇数列=实部I)
csvFile = 'datasets/csi_20261103_120000_data.csv';
M = readmatrix(csvFile);

iqStart = 13;                          % iq_0000 所在列
iqRaw   = M(:, iqStart:end);           % N x 384
csi     = complex(double(iqRaw(:, 2::2)), ...  % 实部 I = 奇数列
                  double(iqRaw(:, 1:2:end)));  % 虚部 Q = 偶数列
amp     = abs(csi);                    % N x 192 幅度
phase   = angle(csi);                  % N x 192 相位
rssi    = M(:, 6);
tRel    = M(:, 3);                     % 相对时间(秒)

figure('Name', 'CSI dataset from flat CSV');
subplot(2,1,1);
imagesc(amp); colorbar; colormap parula;
xlabel('subcarrier index'); ylabel('packet'); title('|CSI| 幅度热力图');
% 子载波布局: 1:64=LLTF, 65:128=HT-LTF, 129:192=STBC-HT-LTF
subplot(2,1,2);
plot(tRel, amp(:, 11)); grid on;
xlabel('time (s)'); ylabel('|CSI|'); title('子载波 11 幅度时序');

%% ========== 方式二: mat 文件 (csi_logger.py --mat 生成) ==========
matFile = 'datasets/csi_20261103_120000_data.mat';
S = load(matFile);
% S.csi    : N x 192 复数子载波 (single; 仅 --all-ltfs 时为 192, 默认 N x 64 只含 HT-LTF 段)
% S.amp    : N x 192 幅度
% S.phase  : N x 192 相位
% S.iq     : N x 384 int8 原始值
% S.rssi / S.t_rel / S.local_timestamp_us : N x 1 元数据
% S.label  : N x 1 元胞数组, 每包的标签
fprintf('mat 数据: %d 包 x %d 子载波, 标签: %s\n', ...
        size(S.csi, 1), size(S.csi, 2), strjoin(unique(S.label), ', '));

figure('Name', 'CSI dataset from .mat');
imagesc(abs(S.csi)); colorbar;
xlabel('subcarrier index'); ylabel('packet');
title(sprintf('|CSI| 热力图 (RSSI 均值 %.1f dBm)', mean(S.rssi)));
