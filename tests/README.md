# API Tests

这个目录主要测试仓库根目录下的 API 层，也就是 `FastAPI -> SimManager -> SimWorker` 这条链路中的 API 侧行为。

和 [simworker/tests/README.md](/root/simworker_service/simworker/tests/README.md) 不同，这里的重点不是直接测 `SimWorker` 协议本身，而是：

- FastAPI 路由是否正确暴露
- API 层响应体是否符合当前约定
- API 层是否把请求正确转发给 `SimManager`
- MJPEG HTTP 视频流是否能正常工作
- 在开启真实环境时，API 层整链路是否能真实跑通

## 测试文件与数量

当前这个目录下主要有 1 个测试文件：

- [test_api_app.py](/root/simworker_service/tests/test_api_app.py)

当前共有 13 个测试用例：

- 11 个默认测试
  主要使用 `FakeSimManager` 或局部辅助函数，不要求真实 GPU 环境
- 2 个真实集成测试
  需要真实启动 `SimWorker`，因此要求 GPU 和 Isaac Sim 环境可用

另外还有一个辅助文件：

- [conftest.py](/root/simworker_service/tests/conftest.py)
  这个文件会把仓库根目录加进 `sys.path`，保证 `api` 和 `simworker` 可以被正确 import

## 每个测试在测什么

### 基础接口与响应结构

#### `test_health_endpoint_returns_ok_payload`

验证 `/health` 是否返回 API 当前状态摘要，包括：

- `worker`
- `table_env`
- `objects`
- `robot`
- `streams`

#### `test_capture_endpoint_returns_json_payload_with_download_urls`

验证 `/cameras/{camera_id}/capture` 不再返回压缩包，而是返回 JSON，并且：

- `rgb_image.ref` 和 `depth_image.ref` 中不直接暴露本地路径
- 改为返回 `download_url`
- `capture.id` 和 `created_at` 字段存在

#### `test_capture_artifact_download_endpoint_returns_binary_files`

验证：

- `GET /captures/{capture_id}/artifacts/rgb`
- `GET /captures/{capture_id}/artifacts/depth`

这两个下载接口是否会返回正确的二进制文件和响应头。

#### `test_capture_artifact_download_endpoint_returns_json_error_for_unknown_capture`

验证当 `capture_id` 不存在时，下载接口是否返回 API 统一的 JSON 错误结构。

### MJPEG 流辅助逻辑

#### `test_stream_response_builder_returns_mjpeg_bytes_and_cleans_up`

验证 `build_mjpeg_streaming_response(...)` 是否能：

- 产出合法的 MJPEG 响应片段
- 启动流
- 在结束时正确清理流

#### `test_stream_response_builder_reference_counts_reused_worker_stream`

验证当两个 HTTP MJPEG 请求复用同一条底层 worker stream 时：

- 第一个请求断开不会提前 stop 底层流
- 只有最后一个请求断开时才会真正 stop

#### `test_open_mjpeg_stream_unregisters_consumer_shared_memory`

验证 `_open_mjpeg_stream(...)` 在打开共享内存时，是否正确执行了 `resource_tracker.unregister(...)`，避免 Python 对共享内存生命周期做错误回收。

### API 到 SimManager 的委托行为

#### `test_table_env_endpoints_delegate_to_sim_manager`

验证下面这些桌面环境接口是否正确委托给 `SimManager`：

- `GET /table-envs`
- `PUT /table-env/current/{table_env_id}`
- `GET /table-env/current/objects`
- `DELETE /table-env/current`

同时也会验证响应体字段是否符合当前 API 文档约定。

#### `test_robot_endpoints_delegate_to_sim_manager`

验证下面这些机械臂相关接口是否正确委托给 `SimManager`：

- `GET /robot/status`
- `GET /robot/api`
- `POST /robot/tasks`

#### `test_worker_restart_endpoint_restarts_same_manager_instance`

验证 `POST /worker/restart` 是否会重启当前 manager，并返回新的健康状态。

### API 错误包装与参数校验

#### `test_sim_manager_errors_are_wrapped_as_ok_false_json`

验证当 `SimManager` 抛出业务错误时，API 是否会统一包装成：

- HTTP 仍为 `200`
- JSON 中 `ok = false`
- `error_message` 为实际错误文本

#### `test_request_validation_errors_are_wrapped_as_ok_false_json`

验证当请求体本身校验失败时，API 是否仍然返回统一的 JSON 错误结构，而不是直接抛出 FastAPI 默认错误页。

### 真实 API 集成测试

#### `test_fastapi_real_integration_exercises_non_stream_interfaces`

这个测试会真实创建 FastAPI 应用，并走完整 API 链路，覆盖：

- `/health`
- `/table-envs`
- `/cameras`
- `/robot/status`
- `/robot/api`
- `/table-env/current/default`
- `/table-env/current/objects`
- `/cameras/table_top/capture`
- `/robot/tasks`
- `/worker/restart`

这个测试主要验证 API 非视频流接口在真实环境下是否能跑通。

#### `test_fastapi_real_integration_streams_mjpeg_frames`

这个测试会真实启动 `uvicorn`，然后通过 HTTP 客户端连接 `/cameras/table_top/stream`。

它主要验证：

- MJPEG 响应头是否正确
- HTTP 端是否能持续收到 JPEG 帧
- 收帧速率是否大于 0
- 客户端断开后，流清理是否最终完成

## 运行前提

这套测试分成两类：

- 默认测试
  不要求真实 GPU，也不要求真实启动 Isaac Sim worker
- 真实集成测试
  需要 GPU、Isaac Sim，以及可用的 `SimWorker` 启动解释器

真实集成测试默认不会自动运行；只有显式设置环境变量后才会执行。否则会被 `skip`。

## 测试产物目录约定

### 默认放在哪里

这个目录下的测试现在也实现了固定输出目录规则。

默认情况下，测试产物会放在仓库根目录下面：

```text
/root/simworker_service/test_runs/
```

每执行一次完整测试，都会在上面的目录下创建一个“本次运行目录”，目录名格式是：

```text
run_YYYYMMDD_HHMMSS
```

例如：

```text
/root/simworker_service/test_runs/run_20260406_153000/
```

每个测试案例再在这个目录下面使用固定短别名创建自己的子目录。

当前 13 个测试案例对应的目录名固定如下：

- `test_health_endpoint_returns_ok_payload`
  对应 `api_health`
- `test_capture_endpoint_returns_json_payload_with_download_urls`
  对应 `api_capture_json`
- `test_capture_artifact_download_endpoint_returns_binary_files`
  对应 `api_capture_download`
- `test_capture_artifact_download_endpoint_returns_json_error_for_unknown_capture`
  对应 `api_capture_missing`
- `test_stream_response_builder_returns_mjpeg_bytes_and_cleans_up`
  对应 `api_stream_builder`
- `test_stream_response_builder_reference_counts_reused_worker_stream`
  对应 `api_stream_refcount`
- `test_open_mjpeg_stream_unregisters_consumer_shared_memory`
  对应 `api_open_mjpeg_stream`
- `test_table_env_endpoints_delegate_to_sim_manager`
  对应 `api_table_env`
- `test_robot_endpoints_delegate_to_sim_manager`
  对应 `api_robot`
- `test_worker_restart_endpoint_restarts_same_manager_instance`
  对应 `api_worker_restart`
- `test_sim_manager_errors_are_wrapped_as_ok_false_json`
  对应 `api_sim_manager_error`
- `test_request_validation_errors_are_wrapped_as_ok_false_json`
  对应 `api_validation_error`
- `test_fastapi_real_integration_exercises_non_stream_interfaces`
  对应 `api_real_non_stream`
- `test_fastapi_real_integration_streams_mjpeg_frames`
  对应 `api_real_mjpeg`

### 每个案例目录里通常会有什么

不同测试案例生成的内容不完全相同，但常见产物包括：

- `rgb.png`
- `depth.npy`
- `session/`
- `control.sock`
- `api.stdout.log`
- `api.stderr.log`
- `mjpeg_metrics/`

### 能不能手动指定

可以。

这层测试支持通过环境变量手动指定“整次测试运行目录”：

```bash
API_TEST_OUTPUT_ROOT=/abs/path/to/your/run_dir
```

例如：

```bash
API_TEST_OUTPUT_ROOT=/root/simworker_service/test_runs/manual_run_001 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest tests -q
```

设置后，这个目录本身就会被当作“本次运行目录”，然后测试会在它下面继续创建每个案例自己的固定短别名子目录。

## 只跑这个目录的推荐命令

如果你只想跑根目录 `tests` 这一层 API 测试，并且不要求真实 GPU 集成，推荐：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest tests -q
```

如果你想把这次运行的产物固定到仓库内，也可以这样跑：

```bash
API_TEST_OUTPUT_ROOT=/root/simworker_service/test_runs/run_YYYYMMDD_HHMMSS \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest tests -q
```

## 运行真实 API 集成测试的命令

如果你想把这 2 个真实 API 集成测试也一起跑上，需要加上真实环境变量：

```bash
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest tests -q
```

如果你只想跑真实 API 集成测试本身，可以直接用：

```bash
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest tests/test_api_app.py -q -k "real_integration"
```

## 说明

- 这里的大多数测试都是 API 层 mock / 半集成测试，执行速度通常比 `simworker/tests` 快很多。
- 真实集成测试会真实拉起 `SimWorker`，所以耗时会明显增加。
- 如果你同时并行跑很多 GPU 相关测试，MJPEG 或底层流帧率统计可能会受到资源竞争影响；更稳妥的方式仍然是串行执行。
