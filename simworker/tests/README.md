# SimWorker Tests

这个目录只测试 `simworker` 包本身，不包含仓库根目录 `tests/` 里那套 FastAPI 链路测试。

这里的测试重点是两层：

- `SimWorker` 进程本身能不能被真实拉起，并正确驱动 Isaac Sim 场景、相机、视频流和任务执行。
- `SimManager` 这一层能不能正确拉起 `SimWorker`，并通过 UDS 正常调用所有核心接口。

## 测试文件与数量

当前这个目录下共有 2 个测试文件、6 个测试用例：

- [test_simworker_integration.py](/root/simworker_service/simworker/tests/test_simworker_integration.py)
  共 5 个测试，直接面向 `SimWorker` 进程做真实集成测试。
- [test_sim_manager_integration.py](/root/simworker_service/simworker/tests/test_sim_manager_integration.py)
  共 1 个测试，面向 `SimManager -> SimWorker` 这条控制链路做真实集成测试。

## 每个测试在测什么

### `test_simworker_integration.py`

#### `test_simworker_default_env_simple_interfaces_and_two_camera_snapshots`

验证 `default` 桌面环境下的一组基础接口是否都能正常工作，包括：

- `hello`
- `list_table_env`
- `list_api`
- `list_camera`
- `get_robot_status`
- `load_table_env("default")`
- `get_table_env_objects_info()`
- `get_camera_info("table_top")`
- `get_camera_info("table_overview")`

同时会校验：

- `default` 环境是否返回 `red_cube`、`blue_cube`
- 对象返回结构里是否包含 `pose`、`bbox_size_xyz_m`、`geometry`、`color`
- 两个相机的 RGB / depth artifact 是否真实落盘

#### `test_simworker_ycb_env_simple_interfaces_and_two_camera_snapshots`

和上一个测试流程基本相同，但场景换成 `ycb`。

额外校验：

- `ycb` 环境是否返回 `cracker_box_1`、`mustard_bottle_1`
- `geometry.type` 是否为 `mesh`

注意：

- 如果本机不存在 YCB 资产目录，这个测试会被 `skip`
- 当前接受的资产目录是：
  - `/root/Download/YCB/Axis_Aligned_Physics`
  - `/root/Downloads/YCB/Axis_Aligned_Physics`

#### `test_simworker_multi_geometry_env_simple_interfaces_and_two_camera_snapshots`

和前两个基础接口测试流程相同，但场景换成 `multi_geometry`。

额外校验：

- 是否返回完整的 8 个对象：
  - `left_plate`
  - `right_plate`
  - `red_cube`
  - `blue_cube`
  - `green_block`
  - `yellow_block`
  - `purple_cylinder`
  - `orange_cylinder`

这个测试主要用于验证多几何体场景的对象信息返回是否正确。

#### `test_simworker_default_env_two_camera_snapshots_and_dual_streams`

这个测试在 `default` 环境基础上，进一步验证双路视频流能力。

主要检查：

- `table_top` 和 `table_overview` 两路流是否都能启动
- 对同一个相机重复调用 `start_camera_stream` 时，是否复用已有流
- 共享内存头是否有效
- 两路流的实际帧率是否落在当前预期范围内
- 每路流是否能连续取样并保存 PNG
- 停止流后，`active_count` 是否正确变化
- 停止流后，对应共享内存是否被正确清理

#### `test_simworker_default_env_run_task_keeps_dual_streams_publishing`

这个测试在 `default` 环境里同时启动双路视频流，再执行一次真实 `run_task`。

主要检查：

- `run_task` 执行前，两路流的基线 FPS 是否正常
- 任务执行期间，两路流是否仍在持续发布
- `run_task` 执行结束后，任务状态是否为 `succeeded`
- 任务执行结束后，机械臂状态是否回到 `idle`

这个测试主要用来防止出现“任务执行时视频流卡死”这一类回归。

### `test_sim_manager_integration.py`

#### `test_sim_manager_default_env_exercises_all_interfaces`

这个测试不直接连 `SimWorker` socket，而是通过 `SimManager` 去拉起并控制 worker。

它会覆盖：

- `SimManager.hello()`
- `SimManager.list_table_env()`
- `SimManager.list_api()`
- `SimManager.list_camera()`
- `SimManager.get_robot_status()`
- `SimManager.load_table_env("default")`
- `SimManager.get_table_env_objects_info()`
- `SimManager.get_camera_info(...)`
- `SimManager.start_camera_stream(...)`
- `SimManager.run_task(...)`
- `SimManager.stop_camera_stream(...)`
- `SimManager.shutdown()`

它除了验证功能本身，还会额外记录一份完整的 UDS 请求/响应 trace，用来确认 `SimManager` 发给 worker 的控制命令序列是否正确。

## 运行前提

这套测试不是纯单元测试，而是真实集成测试。运行前需要满足下面几个条件：

- 机器已经可用 GPU
- Isaac Sim 环境可正常启动
- `SimWorker` 要用 Isaac Sim 自带解释器启动
- 当前仓库根目录需要加入 `PYTHONPATH`

另外，这个目录下的测试默认不会自动运行；只有显式设置环境变量后才会执行。否则会被 `skip`。

## 测试产物目录约定

### 默认放在哪里

默认情况下，`simworker/tests` 这一层测试生成的文件会放在 `simworker` 目录下面：

```text
<repo>/simworker/test_runs/
```

在当前仓库里，对应的默认位置就是：

```text
/root/simworker_service/simworker/test_runs/
```

### 一次完整测试运行的目录叫什么

每执行一次完整测试，都会在上面的目录下创建一个“本次运行目录”，目录名固定采用下面这个格式：

```text
run_YYYYMMDD_HHMMSS
```

例如：

```text
/root/simworker_service/simworker/test_runs/run_20260406_153000/
```

### 每个测试案例的目录叫什么

在“本次运行目录”下面，每个测试案例都会再创建一个自己的子目录。

当前约定的目录名不是完整测试函数名，而是固定的短别名。这样做的原因是：

- 目录仍然可读
- 但不会把 Unix Domain Socket 路径撑得太长

当前 6 个测试案例对应的目录名固定如下：

- `test_simworker_default_env_simple_interfaces_and_two_camera_snapshots`
  对应 `sw_default_simple`
- `test_simworker_ycb_env_simple_interfaces_and_two_camera_snapshots`
  对应 `sw_ycb_simple`
- `test_simworker_multi_geometry_env_simple_interfaces_and_two_camera_snapshots`
  对应 `sw_multi_geometry_simple`
- `test_simworker_default_env_two_camera_snapshots_and_dual_streams`
  对应 `sw_default_streams`
- `test_simworker_default_env_run_task_keeps_dual_streams_publishing`
  对应 `sw_default_run_task`
- `test_sim_manager_default_env_exercises_all_interfaces`
  对应 `sm_default_all`

### 每个案例目录里通常会有什么

不同测试案例生成的内容不完全相同，但通常会包含下面这些文件或子目录：

- `session/`
  `SimWorker` 自己的 session 目录；相机 artifact 也会落在这里面
- `control.sock`
  当前测试案例对应的 UDS socket
- `simworker.log`
  直接启动 `SimWorker` 时的日志
- `saved_payloads/`
  某些案例会把对象信息 payload 落盘到这里
- `stream_samples/`
  流接口测试保存的采样 PNG 和帧率统计
- `run_task_stream_samples/`
  `run_task` 执行期间的流采样结果
- `sim_manager_uds_trace.json`
  `SimManager` 集成测试记录的 UDS 请求/响应 trace
- `sim_manager_saved_payloads/`
  `SimManager` 层保存的对象信息 payload
- `sim_manager_run_task_stream_samples/`
  `SimManager` 层 `run_task` 期间的流采样结果

## 能不能手动指定

可以，但当前只支持手动指定“整次测试运行的根目录”，不支持给每个测试案例单独指定不同的目录名。

如果你想手动指定整次运行目录，可以设置：

```bash
SIMWORKER_TEST_OUTPUT_ROOT=/abs/path/to/your/run_dir
```

例如：

```bash
SIMWORKER_TEST_OUTPUT_ROOT=/root/simworker_service/simworker/test_runs/manual_run_001
```

设置后，这个目录本身就会被当作“本次运行目录”，然后测试会在它下面继续创建每个案例自己的固定短别名子目录。

## 只跑这个目录的推荐命令

如果你只想测试 `simworker/tests` 这个目录，推荐直接用下面这条命令：

```bash
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTHONPATH=/root/simworker_service \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest simworker/tests -q
```

这几个环境变量的作用分别是：

- `SIMWORKER_RUN_ISAACSIM_TESTS=1`
  打开真实 Isaac Sim 集成测试；不设的话，大部分测试会直接 `skip`
- `SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh`
  指定用 Isaac Sim 自带 Python 拉起 worker
- `PYTHONPATH=/root/simworker_service`
  让 pytest 和子进程都能正确 import 当前仓库下的 `simworker`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
  避免系统里其他 pytest 插件干扰当前测试环境

如果你想手动指定这次运行的输出目录，可以在上面的命令前面再加一个环境变量：

```bash
SIMWORKER_TEST_OUTPUT_ROOT=/root/simworker_service/simworker/test_runs/manual_run_001 \
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTHONPATH=/root/simworker_service \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest simworker/tests -q
```

## 只跑单个文件的命令

如果你只想跑 `SimWorker` 这一层：

```bash
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTHONPATH=/root/simworker_service \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest simworker/tests/test_simworker_integration.py -q
```

如果你只想跑 `SimManager` 这一层：

```bash
SIMWORKER_RUN_ISAACSIM_TESTS=1 \
SIMWORKER_TEST_PYTHON=/root/isaacsim/python.sh \
PYTHONPATH=/root/simworker_service \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run pytest simworker/tests/test_sim_manager_integration.py -q
```

## 说明

- 这里的测试会真实启动 Isaac Sim，因此整体耗时会明显高于普通单元测试。
- `ycb` 相关测试在资产目录不存在时会跳过，这是当前设计的一部分，不算失败。
- 如果同时并行跑很多 GPU 相关测试，个别视频流 FPS 断言可能会受到资源竞争影响；更稳妥的方式是串行跑这个目录。
