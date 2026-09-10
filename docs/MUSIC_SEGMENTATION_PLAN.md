# 音乐人声片段切分方案

## 目标

将歌曲切分为适合终端播放的短片段，满足：

- 单个片段最长不超过 30 秒；
- 不在一句歌词中间截断；
- 片段开头和结尾都落在人声位置；
- 中间尽量保持连续人声；
- 非人声占比不超过 30%；
- 遇到较长伴奏段时结束当前片段，不强行填满 30 秒。

## 本机实验配置

当前电脑为 Intel i5-13500H、16 GB 内存、Intel Iris Xe 集成显卡，没有 NVIDIA CUDA。因此采用 CPU 顺序处理：

1. FFmpeg 将输入音频转换为 16 kHz 单声道 WAV；
2. Demucs `htdemucs` 分离 vocal 和 accompaniment；
3. faster-whisper 使用 `small` 或 `medium` 获取歌词及时间戳；
4. 将歌词句子边界与 vocal 能量区间合并；
5. 对候选片段计算人声占比、开头/结尾人声评分和最长伴奏间隔；
6. 丢弃不满足条件的片段，合格片段转码为 Opus，并生成 JSON 清单。

## 候选片段规则

建议以 20～40 ms 帧计算人声活动，候选片段至少满足：

```text
duration <= 30s
vocal_ratio >= 70%
start_voice_score >= 0.7
end_voice_score >= 0.7
longest_instrumental_gap <= 1.5~2s
```

Whisper 时间戳只用于确定歌词句子边界，最终边界还要用 vocal 能量重新校正，避免歌曲 ASR 时间戳漂移造成截断。

## 实验一

输入歌曲：`山风山风等等我`，歌手：王佳。

实验顺序：先检查本地依赖和音频元数据，再执行人声分离；确认分离质量后再接入歌词识别和片段生成。所有实验产物放在 `backend/data/music/working/`，该目录不纳入 Git。

## 实验一结果

输入文件时长约 209.5 秒。CPU 人声分离耗时约 3 分 32 秒，得到 `vocals.wav`
和 `no_vocals.wav`。`faster-whisper small` 在 vocal 音轨上成功识别中文歌词并给出
稳定的重复段落时间戳。

初步按歌词完整边界生成了 6 个候选 Opus 片段：

| 片段 | 时间范围 | 时长 | 人声活动不足帧比例 | 人声/总能量均值 |
| --- | ---: | ---: | ---: | ---: |
| seg-001 | 20.28–46.18 | 25.90s | recheck | recheck |
| seg-002 | 49.36–77.40 | 28.04s | 17.8% | 0.553 |
| seg-003 | 82.68–101.98 | 19.30s | 16.0% | 0.591 |
| seg-004 | 113.16–142.24 | 29.08s | 26.4% | 0.492 |
| seg-005 | 142.24–170.36 | 28.12s | 19.1% | 0.554 |
| seg-006 | 175.60–194.68 | 19.08s | 15.2% | 0.590 |

结论：本机性能足以完成处理，Whisper 时间戳适合作为“不截断歌词”的边界依据；
但 Demucs 分离后的简单能量比偏低，不能直接使用 `vocal_ratio >= 70%` 作为唯一
淘汰条件。下一步应结合歌词覆盖率、连续人声间隔和分离音轨的校准结果评分，再决定
是否保留片段。当前候选音频只存在于被忽略的工作目录，没有写入正式音乐目录。
word starts immediately with no pause, the boundary must be rejected. Candidate boundaries
must combine word timestamps with a short vocal pause. In this experiment `seg-001` was
shortened from 49.36s to 46.18s because 49.36s cut into the next lyric phrase.
word starts immediately with no pause, the boundary must be rejected. Candidate boundaries
must combine word timestamps with a short vocal pause. In this experiment `seg-001` was
shortened from 49.36s to 46.18s because 49.36s cut into the next lyric phrase.
## 实验一结论（2026-09-11）

已使用歌曲《山风山风等等我》进行本地实验验证：

- 先用 Demucs `htdemucs` 分离人声与伴奏，再在人声轨道上使用 faster-whisper 获取歌词词级时间戳。
- 按连续歌词和人声活动区间生成不超过 30 秒的 Opus 片段。
- 初始方案仅按固定时长切分时，可能在仍有歌词的位置截断句子；例如第一段在 `49.36s` 处切断了连续歌词。
- 修正为结合词级时间戳和停顿边界后，将第一段结束点调整到 `46.18s`，避免截断完整歌词。
- 已试听修正后的第一段、第二段和第三段，第三段约 19 秒，试听效果可接受，当前方案具备继续扩展的基础。

后续正式处理时，边界选择应优先级如下：完整歌词句尾/词尾 > 明显人声停顿 > 伴奏低谷；若单段超过 30 秒，则在最近的自然停顿处拆分。

## 可复用流程与当前本地音乐库（2026-09-11）

后续新增歌曲时，复用以下步骤：

1. 将源音频放入 `backend/data/music/audio/`，并在 `working/htdemucs/` 下做人声分离。
2. 使用 faster-whisper 在 `vocals.wav` 上生成词级时间戳。
3. 按连续歌词分组，结合人声停顿修正首尾，单段不超过 30 秒。
4. 转为 24 kHz、单声道 Opus，试听确认后再复制到正式 `audio/` 目录。
5. 在 `catalog.json` 中为每个片段建立独立条目，保留歌曲、歌手和来源信息。

本次已将本地音乐库替换为四首歌曲的切分片段，共 19 段：

- 《山风山风等等我》：6 段
- 《中华民谣》：7 段
- 《小跳蛙》：5 段
- 《小毛驴》：1 段

原有 10 首 ccMixter 音频及其 Opus 缓存已从本地可播放目录移除。下载源的授权状态仍需在正式对外使用前确认。
