# vLLM 服务指标：Qwen/Qwen3-8B

- Qwen/Qwen3-8B（权重 15.26 GiB，tensor parallel 2）：measured

GPU：Tesla T4, Tesla T4；vLLM 0.31.0；启动 340.1 秒；测量工具：builtin client。
负载：每个请求约 1536 个输入 token、固定 128 个输出 token（ignore_eos），enable_thinking = false，每档至少 40 个请求。
显存（MiB，每张卡）：加载前 [0, 0]，加载后 [13641, 13641]，压测峰值 [13905, 13905]，总量 [15360, 15360]。vLLM 启动时按 gpu_memory_utilization = 0.9 预留权重和 KV cache，所以这个数字是预留量；权重本身占多少看下面的日志行。

    (Worker_TP0 pid=349) INFO 11-26 09:59:55 [weight_utils.py:1063] Filesystem type for checkpoints: OVERLAY. Checkpoint size: 15.26 GiB. Available RAM: 3.51 GiB.
    sabled because the filesystem (OVERLAY) is not a recognized network FS (NFS/Lustre) and the checkpoint size (15.26 GiB) exceeds 90% of available RAM (3.51 GiB).
    (Worker_TP1 pid=350) INFO 11-26 10:00:51 [model_runner.py:407] Model loading took 7.64 GiB memory and 133.279883 seconds
    (Worker_TP0 pid=349) INFO 11-26 10:00:51 [model_runner.py:407] Model loading took 7.64 GiB memory and 133.352083 seconds
    (EngineCore pid=290) INFO 11-26 10:00:52 [utils.py:343] Using LBNHC KV cache layout.
     found in 60 seconds. This typically happens when some processes are hanging or doing some time-consuming work (e.g. compilation, weight/kv cache quantization).
    (Worker_TP1 pid=350) INFO 11-26 10:02:01 [model_runner.py:1087] Graph capturing finished in 14 secs, took 0.41 GiB
    (Worker_TP0 pid=349) INFO 11-26 10:02:01 [model_runner.py:1087] Graph capturing finished in 14 secs, took 0.41 GiB

| 并发 | 完成 / 请求 | TTFT p50 / p95 (ms) | TPOT p50 / p95 (ms) | 端到端 p50 / p95 (ms) | 输出 token/s | 请求/s | 显存峰值 (MiB) |
|---|---|---|---|---|---|---|---|
| 1 | 40 / 40 | 1125.96 / 1208.71 | 38.89 / 40.2 | 6067.44 / 6243.99 | 21.19 | 0.166 | [13765, 13765] |
| 2 | 40 / 40 | 1767.0 / 2344.11 | 47.6 / 51.86 | 7772.89 / 7854.72 | 32.86 | 0.257 | [13767, 13767] |
| 4 | 40 / 40 | 2263.97 / 3506.97 | 67.43 / 76.49 | 10913.5 / 11117.87 | 46.81 | 0.366 | [13839, 13839] |
| 8 | 40 / 40 | 3381.63 / 7191.19 | 106.22 / 124.22 | 17043.0 / 19302.92 | 59.95 | 0.468 | [13839, 13839] |
| 16 | 80 / 80 | 4589.69 / 15102.23 | 194.25 / 219.86 | 29738.04 / 38951.38 | 69.13 | 0.54 | [13839, 13839] |
- vllm bench serve not usable, built-in client used instead: `vllm bench serve` has no --backend, --endpoint, --base-url, --model, --dataset-name, --random-input-len, --random-output-len, --num-prompts, --max-concurrency, --save-result, --result-dir, --result-filename
