# 踩过的坑

每条都带实测证据或上游源码位置。没实测过的写「未验证」。

## faster-whisper 1.2.1 在 PyAV 19 上崩溃

报错：

```
TypeError: open() got an unexpected keyword argument 'metadata_errors'
  with av.open(input_file, mode="r", metadata_errors="ignore") as container:
```

`metadata_errors` 是 PyAV 19 删掉的参数。上游 master 已修（按版本决定传不传），
但截至 1.2.1 未发版，所以升级解决不了。

**本包的绕法：不走 `av.open`。** `local.wav_samples` 用标准库 `wave` 读出 16 bit
PCM，转成 float32 数组，交给 `model.transcribe(array)`。这是上游支持的输入形态，
解码路径整个跳过。ffmpeg 那一步早就把音频转成 16 kHz 单声道 WAV，本来也不需要 PyAV。

实测来源：`EcoBuddy/docs/research/measured-local-asr-benchmark.md` 第三节，
2026-10 在同一台机器上确认。

## ctranslate2 在 Windows 上找不到 CUDA 运行库

报 `Could not load library cublas64_12.dll` 或 `cudnn_ops64_9.dll`。pip 装的
`nvidia-*-cu12` 把 DLL 放在 `site-packages/nvidia/<库>/bin`，不在 PATH 上。
`local._preload_cuda_libs` 在 import ctranslate2 之前把这些目录加进
`os.add_dll_directory` 与 PATH。没有 CUDA toolkit 的机器也能跑。

## `PYTHONIOENCODING` 让 probe 崩

见 `docs/bilibili-pipeline.md`「已知限制」第一条。与本仓的本地链路无关，但同一环境里
跑两个子命令时容易撞上。

## 控制台是 cp936

讲次标题来自平台，可能含 U+20000 或 emoji。`main` 把自己的两个流包一层替换降级，
不改 `sys.stdout`。本地链路同理：文件名可能含中文，报告打印前已降级。
