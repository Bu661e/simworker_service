## IsaacSim Worker 详细设计

本文档描述新架构下 `IsaacSim Worker` 的设计。这里的 `worker` 指被 `SimManager` 拉起的 Isaac Sim 独立进程，负责仿真环境初始化、场景对象管理、相机采集、任务执行和原始视频帧生产。

这里先强调一个当前实现中的硬约束：

- 整个 simworker 在运行时只有一个 `Worker` 进程。
- 这个 `Worker` 进程就是唯一的 Isaac Sim 执行进程。
- 不再额外拆出第二个 `Worker` 进程去负责 stream、渲染或 world step。
- `SimManager` 只负责拉起和管理这一唯一的 `Worker` 进程。

当前设计目标有三个：

- 将控制协议与 Isaac Sim 日志输出彻底分离，避免 stdout 污染协议解析。
- 保持 `worker` 只负责仿真和执行，`SimManager` 负责会话管理、生命周期管理和对外接口聚合。
- 将控制面与数据面拆分：小消息走控制协议，大体积图片、深度图、视频帧走独立通道。

### 1. 启动方式

`worker` 必须由 Isaac Sim 安装目录下的 `python.sh` 启动，而不是直接使用系统 Python。原因是 Isaac Sim 的 Python 依赖、Kit 环境变量和扩展路径都需要由 `python.sh` 预先准备。

推荐启动方式如下：

```bash
$ISAAC_SIM_ROOT/python.sh /path/to/robot_service/simworker/entrypoint.py \
  --session-dir <session_dir> \
  --control-socket-path <socket_path>
```

当前仓库内建议 API 层直接复用 `simworker/sim_manager.py` 中的 `SimManager`，而不是自行重复实现一套 `subprocess + socket` 管理逻辑。

`SimManager` 的职责建议固定为：

- 维护 API 层全局唯一的一份 `worker` 进程句柄。
- 创建 `session_dir` 与 `control_socket_path`。
- 通过 Isaac Sim 的 `python.sh` 拉起 `worker`。
- 用 `hello` 作为 ready probe，确认 `worker` 已经可用。
- 对 API 层暴露 `hello`、`list_table_env`、`list_camera`、`load_table_env`、`get_table_env_objects_info`、`get_robot_status`、`get_camera_info`、`start_camera_stream`、`stop_camera_stream`、`run_task`、`shutdown` 等高层方法。
- `SimManager` 可以保留启动超时、关闭超时和单次请求超时等外层兜底；如果某次请求在限定时间内一直没有返回，`SimManager` 直接终止当前 `worker` 进程，而不是再额外发送一条 `shutdown` 命令。
- `SimManager` 只负责控制面，不负责 stream 数据面的读取实现；例如打开 shared memory、读取 `rgb24` 帧、解析 `latest_frame_v1` header、封装 reader SDK 等，都应由 API 层自行处理。

推荐用法示例如下：

```python
from simworker.sim_manager import SimManager

sim_manager = SimManager(
    session_dir="/tmp/simworker-session",
    control_socket_path="/tmp/simworker.sock",
    python_bin="/root/isaacsim/python.sh",
)

sim_manager.hello()
sim_manager.list_table_env()
sim_manager.list_camera()
sim_manager.load_table_env("default")
camera_info = sim_manager.get_camera_info("table_top")
stream_info = sim_manager.start_camera_stream("table_top")
sim_manager.stop_camera_stream(stream_info["stream"]["id"])
```

建议约定如下启动参数：

- `--session-dir`
  当前会话根目录。`worker` 会在其中创建本次运行目录，用于存放日志、artifact、临时文件和调试数据。
- `--control-socket-path`
  `worker` 与 `SimManager` 之间的 Unix domain socket 路径，用于控制面通信。

这里需要特别强调：

- `control-socket-path` 只用于控制面通信。
- `control-socket-path` 不传输图片、深度图、视频帧或其他二进制文件本体。
- `control-socket-path` 只传输结构化 JSON 控制消息，以及 artifact 引用、stream 引用等元数据。
- 如果后续实现里需要传输图片、深度图或视频，这些数据必须走独立数据面，而不是复用 `control-socket-path`。
- `SimManager` 在 stream 相关场景下也只负责发起 `start_camera_stream` / `stop_camera_stream` 并返回 `stream.ref`；它本身不应再承担数据面 reader 的职责。
- 当前实现里同一时刻只允许存在一个 `Worker` 进程；控制命令、world step、相机快照和 stream 刷新都在这个唯一 `Worker` 进程内完成。
- `worker` 启动阶段会先加载固定基础环境，包括地面、灯光、桌子、机械臂和默认相机；这部分是实现前提，不需要单独暴露成控制面状态字段。
- `hello` 里的 `objects.object_count` 只统计后续通过 `load_table_env` 加入的桌面对象，不包含基础环境元素。

启动过程建议分为以下阶段：

1. `SimManager` 创建 `session_dir`，并确定 `control_socket_path`。
2. `SimManager` 通过 Isaac Sim 的 `python.sh` 拉起 `worker`。
3. `worker` 创建本次运行目录 `run_dir = session_dir/YYYY-MM-DD_HH-MM-SS/`，初始化日志，然后创建 `SimulationApp({"headless": True})`。
4. `worker` 在启动阶段完成固定基础环境初始化，包括地面、灯光、桌子、机械臂和默认相机；但不在此阶段自动创建任何桌面对象。
5. `worker` 绑定并监听 `control_socket_path`。
6. `SimManager` 连接 `control_socket_path`。
7. `SimManager` 发送 `hello` 请求，作为 ready probe。
8. `worker` 返回 `ok: true` 且 `payload.worker.status = "ready"` 后，控制面开始正常收发命令；此时基础环境已经可用，但 `payload.table_env.loaded` 仍然可能为 `false`，直到后续收到 `load_table_env`。

### 2. worker 与 SimManager 的交互协议

#### 2.1 总体原则

`worker` 与 `SimManager` 的交互采用：

- 控制面：`Unix domain socket + length-prefixed JSON`
- 数据面：
  - artifact 文件：保存在 `session_dir/YYYY-MM-DD_HH-MM-SS/artifacts/`
  - 视频流：由 `worker` 维护内部流引用，数据面默认采用同机 latest-frame buffer

其中：

- `SimManager` 是控制发起方和结果汇总方。
- `worker` 是仿真执行方和原始数据生产方。

这里再明确一次边界：

- 控制面 socket 只负责命令、状态、结果和引用信息。
- 控制面 socket 不负责传输任何大块二进制内容。
- 图片、深度图、视频流都属于数据面，不允许直接塞进控制 socket。

#### 2.2 为什么不用 JSON line 作为主协议

原因是 Isaac Sim 和 Kit 在启动与运行过程中会向 stdout/stderr 输出日志，这会污染控制消息流。

改用 Unix domain socket 后：

- Isaac Sim 日志继续写入日志文件或 stdout/stderr。
- 控制协议只走 socket。
- `SimManager` 不需要再从日志里筛选 JSON。

#### 2.3 帧格式

每条控制消息格式如下：

```text
[4-byte big-endian length][JSON payload bytes]
```

其中 JSON payload 统一采用 UTF-8 编码。

这样做的好处是：

- 不依赖换行符，消息边界更清晰。
- 可以安全承载较长的 JSON payload。
- socket 作为纯协议通道，不会混入日志文本。

#### 2.4 基本消息结构

请求消息统一使用以下 JSON 结构：

```json
{
  "request_id": "req-001",
  "command_type": "get_camera_info",
  "payload": {
    "camera": {
      "id": "table_top"
    }
  }
}
```

成功响应统一使用以下 JSON 结构：

```json
{
  "request_id": "req-001",
  "ok": true,
  "payload": {
    "camera": {
      "id": "table_top"
    }
  }
}
```

错误响应统一使用以下 JSON 结构：

```json
{
  "request_id": "req-001",
  "ok": false,
  "error_message": "camera.id table_top does not exist",
  "payload": {}
}
```

字段约定如下：

- `request_id`
  由 `SimManager` 生成，用于请求响应关联。
- `command_type`
  仅出现在请求里。
- `ok`
  表示本次执行是否成功。
- `error_message`
  失败时返回的错误信息。
- `payload`
  业务数据，统一按资源分组组织。

资源字段命名统一采用以下规则：

- `payload` 优先按资源分组，例如 `worker`、`scene`、`robot`、`camera`、`stream`、`task`。
- 资源标识统一使用 `id`，不再混用 `object_id`、`camera_id`、`stream_id`。
- 资源状态统一使用 `status`。
- 资源分类统一使用 `kind`。
- 位姿统一放在 `pose` 下。
- 输入参数统一放在 `input` 下。
- 输出结果统一放在 `result` 下。
- 间接引用统一放在 `ref` 下。
- 需要补充的非核心信息统一放在可选 `metadata` 下。

公共子结构推荐如下：

```json
{
  "pose": {
    "position_xyz_m": [0.0, 0.0, 0.0],
    "rotation_rpy_deg": [0.0, 0.0, 0.0],
    "rotation_order": "intrinsic_xyz"
  },
  "ref": {
    "id": "artifact-rgb-001",
    "kind": "artifact_file",
    "path": "/session_dir/2026-04-01_10-11-12/artifacts/table_top_rgb_10-11-12.png",
    "content_type": "image/png"
  }
}
```

使用约定如下：

- 同一个资源在请求和响应里尽量保持同一层级和同一字段名。
- 请求中出现的资源 `id`，响应里应尽量原样回显。
- 只有确实需要时才返回 `metadata`，避免重新引入隐式字段。
- 写入类请求中的 `pose` 默认使用 `position_xyz_m + rotation_rpy_deg`。
- 查询类响应中的 `pose` 默认使用 `position_xyz_m + quaternion_wxyz`。
- 这条规则与当前旧代码保持一致：内部配置和挂载时常用欧拉角，对外返回世界位姿时统一返回四元数。

#### 2.5 推荐命令集合

- `hello`
  获取 `worker` 当前整体状态。
- `list_table_env`
  返回当前 `worker` 支持加载的桌面环境列表。
- `load_table_env`
  按 `table_env_id` 加载一套预定义桌面物体。
- `get_table_env_objects_info`
  返回当前桌面环境对象的最新位姿和缩放信息。
- `get_robot_status`
  获取机器人当前状态。
- `list_camera`
  返回当前 `worker` 可用的相机列表。
- `get_camera_info`
  获取相机快照、内参、位姿和 artifact 引用信息。
- `start_camera_stream`
  启动某个相机的内部视频流。
- `stop_camera_stream`
  停止某条已经启动的内部视频流。
- `run_task`
  执行任务。
- `shutdown`
  关闭 `worker`。

### 3. 图片、深度图和视频流的处理方式

本设计中，控制面不直接承载图片和视频字节。

这里的“控制面不直接承载”不是“尽量不要”，而是设计约束：

- 不通过 `control-socket-path` 发送 PNG 文件字节。
- 不通过 `control-socket-path` 发送 `.npy` depth 内容。
- 不通过 `control-socket-path` 发送 MJPEG 帧或 WebRTC 视频帧。
- 控制面只返回这些数据的引用信息、流引用和必要元数据。

#### 3.1 图片和深度图

- `worker` 采集某个摄像头单帧数据。
- RGB 保存为 `.png`，depth 保存为 `.npy`。
- 控制响应中只返回文件引用信息，不返回文件字节。
- 实际的图片和深度文件保存在当前 `run_dir/artifacts/` 目录下。

示例目录结构如下：

```text
session_dir/
  2026-04-01_10-11-12/
    artifacts/
      table_top_rgb_10-11-12.png
      table_top_depth_10-11-12.npy
    worker.log
```

#### 3.2 视频流

- `worker` 的职责限定为“视频帧生产者”，而不是对外视频协议服务器。
- `worker` 负责从 Isaac Sim 相机持续取帧、维护流状态、提供内部数据面。
- API 层或独立发布层负责把内部帧转换成对前端可见的 `MJPEG`、`WebRTC` 或其他协议。

对于当前阶段，更推荐把视频流理解成两层：

- 原始帧层
  - `worker` 从 Isaac Sim 相机获取原始 RGB 帧。
  - `worker` 维护每路相机的最新帧及其元数据。
- 发布层
  - API 层从 `worker` 提供的内部数据面读取最新帧。
  - API 层根据前端需求选择编码和分发协议。

在当前已知前提下，`worker` 与 API 层长期同机部署，实时预览优先服务于单个前端、1 到 2 路相机。因此内部数据面更推荐采用“本地共享内存上的 latest-frame buffer”思路，而不是让 `worker` 直接输出网络视频协议：

- 每路相机对应一条内部流。
- 每条内部流只保留最新帧，不保留完整历史帧队列。
- `worker` 负责持续覆盖写入最新帧。
- API 层按自己的节奏读取当前最新帧；如果来不及消费，则直接丢弃旧帧，只关心最新可用画面。

推荐原则如下：

- `worker` 生产原始帧，不生产对外协议。
- API 层消费原始帧，并决定如何向前端发布。
- 控制面只传流控制信息与元数据，不传帧本体。
- 数据面优先按“同机、最新帧、低耦合”的目标设计。

### 4. 任务执行与并发控制

控制面采用同步请求、同步响应模型。

其中 `run_task` 的语义如下：

- `worker` 在执行任务期间不会继续读取和处理新的控制命令。
- `run_task` 的最终响应会在任务完成或失败后返回。
- 同一时刻只允许有一个任务在执行。
- 如果当前已有相机流在运行，任务执行期间这些视频流仍然继续更新。
- 也就是说，阻塞的是控制面，不是已经启动的数据面流。
- `shutdown`、`hello`、`get_robot_status`、`stop_camera_stream` 等命令在任务执行期间都只能等待任务返回后再处理。
- `task.code` 被视为完全受信的 Python 代码；当前设计不额外引入沙箱、权限裁剪或安全隔离。
- `run_task` 的失败响应不区分 `error_code`；调用方只需要读取 `error_message`。
- 当前设计暂不定义任务超时；如果任务代码长时间不返回，控制面就会持续阻塞等待。

#### 4.1 `world.step` 的归属

为了保证“任务执行”和“视频流更新”可以同时进行，同时又不破坏 Isaac Sim 世界状态一致性，`worker` 内必须遵循以下约束：

- `world.step(...)` 是对整个仿真世界的推进，而不是某个局部模块的方法。
- 同一个 `worker` 进程内，`world.step(...)` 必须只有一个唯一调用点。
- 不允许 `run_task` 的执行逻辑单独维护一套 `world.step(...)` 循环。
- 不允许某个 camera stream 为了取帧再单独维护另一套 `world.step(...)` 循环。
- `run_task` 和 camera stream 共享的是同一个仿真主循环产出的 step，而不是分别拥有自己的 step。

推荐实现为“单 Worker 进程主循环 + 多功能挂接”：

1. `worker` 内部维护一个唯一的主循环。
2. 主循环优先监听控制 socket，按顺序执行收到的控制命令。
3. 当主循环暂时没有收到新的控制命令时，进入一次 idle tick。
4. idle tick 中如果当前有任务在执行，则必须持续调用 `world.step(render=True)` 推进仿真。
5. 如果当前没有任务在执行，但存在活跃 stream 且到了刷新周期，也调用一次 `world.step(render=True)`。
6. step 完成后，如果当前有需要刷新的 stream，再读取相机数据并覆盖写入 latest-frame buffer。
7. `run_task` 的任务控制器也必须挂接在这个唯一主循环上，而不是再拆出第二个执行进程或第二套 step 循环。

这种设计下：

- `run_task` 的控制面请求仍然可以保持同步阻塞，直到任务完成才返回。
- camera stream 仍然可以持续更新，因为它依赖的是同一个 `Worker` 主循环空闲 tick，而不是独立进程、独立线程或第二套 `world.step(...)`。
- 实际联调中，如果把 stream 更新从主 `Worker` 执行路径拆到额外并行执行路径里，容易导致 `hello` 等控制命令超时；因此当前实现明确收敛为“一个 `Worker` 进程 + 一个主循环”。
- 机器人状态、物体状态和相机画面来自同一个 step 序列，因此时序是一致的。

一个推荐的内部伪代码如下：

```python
while worker_running:
    apply_pending_commands()

    should_step_for_task = current_task is not None
    should_step_for_stream = stream_refresh_due(active_streams)

    if should_step_for_task or should_step_for_stream:
        if current_task is not None:
            current_task.tick()

        world.step(render=True)

        if should_step_for_stream:
            for stream in active_streams:
                frame = capture_camera_frame(stream.camera_id)
                stream.write_latest_frame(frame)

    if current_task is not None and current_task.is_finished():
        finalize_task_result(current_task)
        current_task = None
```

`run_task` 的控制面 handler 则更接近下面这种语义：

```python
def handle_run_task(request):
    task = install_task_controller(
        task_id=request.payload["task"]["id"],
        task_code=request.payload["task"]["code"],
        task_objects=request.payload["task"]["objects"],
    )
    wait_until_task_finished(task)
    return build_task_response(task)
```

这里要特别强调：

- `handle_run_task(...)` 本身不负责循环调用 `world.step(...)`。
- camera stream 的后台更新逻辑也不负责循环调用 `world.step(...)`。
- 真正拥有 `world.step(...)` 的是 `worker` 内部唯一的 simulation loop。
- `task.code` 在受控环境里执行后，必须提供统一入口 `run(robot, objects)`。
- 传给 `run(robot, objects)` 的 `objects` 就是请求里给出的原始 `task.objects`，保持 `list[dict]` 结构。
- `run_task` 在执行时只使用这份外部快照，不依赖 `worker` 内部对象查询接口的数据，也不会在执行前替换为内部实时对象状态。

### 5. worker 日志与产物目录

`worker` 每次运行都在 `session_dir` 下创建独立的 `run_dir`。推荐目录结构如下：

```text
session_dir/
  2026-04-01_10-11-12/
    artifacts/
      table_top_rgb_10-11-12.png
      table_top_depth_10-11-12.npy
    worker.log
```

目录约定如下：

- `run_dir` 目录名使用本次运行启动时间。
- `artifacts/` 用于保存图片、深度图和其他文件型产物。
- `worker.log` 用于记录本次运行的业务日志和排障信息。
- 控制面协议消息不通过 stdout 传输，因此日志系统和控制协议互不污染。

### 6. 共享内存 latest-frame 数据面设计

本节进一步细化第 3.2 节中提到的“本地共享内存 latest-frame buffer”实现约束。这里的目标不是把 `worker` 变成流媒体服务器，而是定义一套稳定、低耦合、协议无关的内部帧访问方式。

#### 6.1 设计目标

共享内存数据面应满足以下目标：

- 只服务同机进程间传输，不承担跨机分发职责。
- 每路相机流只保留最新一帧，不保留历史队列。
- `worker` 是单写者，上层消费方按单读者设计。
- 数据面只提供协议无关的原始帧，不直接面向 `MJPEG`、`WebRTC` 等对外协议。
- 控制面只负责流的创建、停止和在相关命令响应中回显当前状态，不直接搬运帧字节。

#### 6.2 基本结构

推荐每条流对应一个独立共享内存对象。一个共享内存对象内部由两部分组成：

- `header`
  固定长度元数据区，用于描述当前最新帧。
- `frame_data`
  可覆盖写入的帧数据区，只保存最新一帧。

推荐逻辑布局如下：

```text
| header (fixed size) | frame_data (fixed capacity) |
```

推荐 `header` 至少包含以下字段：

- `magic`
  固定魔数，用于校验共享内存对象类型。
- `version`
  布局版本，例如 `latest_frame_v1`。
- `seq`
  单调递增序号，用于读写一致性校验。
- `width`
  当前帧宽度。
- `height`
  当前帧高度。
- `pixel_format`
  像素格式，例如 `rgb24`。
- `stride_bytes`
  每行字节数。
- `data_size_bytes`
  当前帧有效字节数。
- `frame_capacity_bytes`
  帧数据区总容量。
- `timestamp_ns`
  当前帧采集时间戳。
- `frame_id`
  当前帧编号。

`frame_data` 区域默认存放协议无关的原始帧字节。当前推荐格式为 `rgb24`，原因是：

- 对 `worker` 最直接，不需要在 `worker` 内部引入额外视频编码职责。
- 对上层发布模块最中立，后续无论转 `MJPEG` 还是 `WebRTC` 都仍然可用。
- 在当前 1 到 2 路、同机部署、latest-frame 优先的约束下，内存带宽成本可接受。

#### 6.3 与控制面字段的对应关系

控制面返回的 `stream.ref` 用于描述如何定位并解释这段共享内存。推荐字段如下：

```json
{
  "stream": {
    "id": "stream-table_top-001",
    "status": "running",
    "buffer_mode": "latest_frame",
    "pixel_format": "rgb24",
    "resolution": [640, 640],
    "ref": {
      "id": "stream-ref-table_top-001",
      "kind": "shared_memory",
      "path": "shm://table_top_main",
      "layout": "latest_frame_v1"
    }
  }
}
```

字段语义如下：

- `stream.id`
  控制面中的流标识。
- `stream.ref.id`
  数据面引用标识。
- `stream.ref.kind`
  固定为 `shared_memory`。
- `stream.ref.path`
  共享内存对象名或可解析句柄。
- `stream.ref.layout`
  用于声明共享内存布局版本。

这里的边界再强调一次：

- `stream.status` 由控制面维护，是流状态的唯一权威表达。
- 共享内存 `header` 只描述帧数据本身，不重复存放一份流状态。
- 上层如果需要知道流是否仍然有效，应以控制面响应结果为准，而不是自行推断共享内存对象状态。

这里建议把 `path` 设计成逻辑引用，而不是在文档里提前绑定某种具体 API 细节。`SimManager` / API 层只需要知道它能用该引用打开一段共享内存，并按 `layout` 解释内容即可。

#### 6.4 生命周期

共享内存流的生命周期建议如下：

1. `start_camera_stream` 到达后，`worker` 为该相机创建或复用一条流。
2. `worker` 创建共享内存对象，初始化 `header` 默认值。
3. `worker` 将该流注册到内部主循环中；后续在主循环空闲 tick 里，如果到了刷新周期，则执行一次共享的 `world.step(render=True)`，并读取该相机最新帧持续覆盖写入 `frame_data`。
4. 上层消费方根据 `stream.ref` 打开共享内存并读取最新帧。
5. `stop_camera_stream` 到达后，`worker` 停止该流写入，并清理共享内存对象。
6. `worker` 退出时，应清理自己创建的所有共享内存对象，避免遗留脏资源。

推荐约定：

- 对同一个相机重复调用 `start_camera_stream` 时，优先复用已有流，而不是重复创建共享内存对象。
- 同一时刻每个相机最多维护一条 latest-frame 流。
- 当前设计不单独提供“查询流状态”命令；流状态只通过 `hello`、`start_camera_stream`、`stop_camera_stream` 等已有命令间接体现。
- `worker` 异常退出后，`SimManager` 应具备补充清理能力，但主清理由 `worker` 负责。
- 当前数据面按单读者设计，不为多读者并发访问定义额外协议保证。
- camera stream 不应拥有独立于 simulation loop 的第二套 `world.step(...)` 调用点。
- 更具体地说，当前实现不允许再拆出第二个 `Worker` 进程去负责 stream 刷新；stream 刷新必须留在唯一的 `Worker` 主循环内执行。

#### 6.5 读写一致性

由于 latest-frame 模式只关注“最新可读帧”，不需要引入完整队列和复杂锁模型。推荐使用基于 `seq` 的无锁一致性约定：

- 写入侧
  - 写入前先递增 `seq`，使其进入“写入中”状态。
  - 写入 `header` 中除 `seq` 外的元数据。
  - 写入 `frame_data`。
  - 写入完成后再次更新 `seq`，使其进入“可读完成”状态。
- 读取侧
  - 先读取一次 `seq_start`。
  - 如果发现当前是“写入中”状态，则重试。
  - 复制 `header` 和 `frame_data`。
  - 再读取一次 `seq_end`。
  - 如果 `seq_start != seq_end`，说明读到了半帧，丢弃并重试。

为了让约定更简单，推荐使用以下规则：

- 奇数 `seq` 表示正在写入。
- 偶数 `seq` 表示当前帧可读。
- 只要读到奇数，或者前后两次 `seq` 不一致，就直接重试。

这种方式适合当前单写者 latest-frame 场景，原因是：

- 不需要维护历史帧。
- 不要求严格保序消费。
- 上层消费方即使偶尔读失败，直接重试即可。

#### 6.6 容量与格式约束

为了避免共享内存对象频繁重建，建议在启动流时按最大预期分辨率一次性分配 `frame_capacity_bytes`。例如当前如果固定为 `640 x 640 x rgb24`，则一帧最大字节数约为：

```text
640 * 640 * 3 = 1,228,800 bytes
```

推荐约定：

- `frame_capacity_bytes` 在流生命周期内固定。
- `data_size_bytes` 表示当前帧实际有效字节数。
- 如果未来允许动态分辨率，分辨率变化必须同步更新 `header.width`、`header.height`、`header.stride_bytes` 和 `header.data_size_bytes`。
- 如果当前帧大小超过 `frame_capacity_bytes`，应将流状态置为 `error` 并停止继续写入，而不是静默截断。

#### 6.7 失败与清理策略

共享内存流至少要覆盖以下异常情况：

- 相机不存在或初始化失败
  - `start_camera_stream` 返回 `ok: false`。
- 共享内存创建失败
  - `start_camera_stream` 返回 `ok: false`，并记录错误日志。
- 采集中途出错
  - 将 `stream.status` 置为 `error`。
  - 保留最后一帧是否可继续读，取决于具体实现，但控制面状态必须可见。
- `worker` 退出
  - 主动停止所有流并清理共享内存对象。

设计原则如下：

- 不把共享内存错误隐藏成“黑屏但控制面正常”。
- 控制面负责暴露流状态，数据面负责提供帧访问。
- 共享内存对象的创建、所有权和销毁边界必须清晰。
- 协议保持最小化，优先保证稳定读写，不为未来可能的多读者复杂场景提前引入额外控制字段。

### 7. worker 各个命令详细内容

本节中所有命令都遵循第 2.4 节定义的统一消息结构。

除 `run_task` 的控制面阻塞特性外，其他命令都按普通同步请求、同步响应处理。

#### 7.1 `hello`

`hello` 用于获取当前 `worker` 的整体运行状态，通常用于探活、启动完成确认和基础状态检查。

请求示例：

```json
{
  "request_id": "req-001",
  "command_type": "hello",
  "payload": {
    "worker": {}
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-001",
  "ok": true,
  "payload": {
    "worker": {
      "status": "ready"
    },
    "table_env": {
      "loaded": false,
      "id": null
    },
    "objects": {
      "object_count": 0
    },
    "robot": {
      "status": "idle",
      "current_task_id": null
    },
    "streams": {
      "active_count": 0
    }
  }
}
```

字段说明：

- `worker.status` 表示当前 `worker` 自身状态，例如 `starting`、`ready`、`error`、`shutting_down`。
- `table_env.loaded` 表示当前是否已经加载某套预定义桌面环境。
- `table_env.id` 表示当前已加载的桌面环境标识；如果尚未加载，则为 `null`。
- `objects.object_count` 表示当前 `worker` 已加载的桌面对象数量。
- `robot.status` 表示机器人当前状态。
- `robot.current_task_id` 表示当前正在执行的任务；如果没有任务在运行，可为 `null`。
- `streams.active_count` 表示当前已启动的视频流数量。

这里需要特别强调：

- `hello` 的 `ready` 只表示 `worker` 控制面和基础运行时已就绪，不表示桌面对象已经存在。
- 在首次 `load_table_env` 之前，返回 `table_env.loaded = false`、`table_env.id = null`、`objects.object_count = 0` 是正常行为，不应视为启动失败。

#### 7.2 `list_table_env`

`list_table_env` 用于返回当前 `worker` 支持的桌面环境列表，供 API 层或上层控制面先发现可用的 `table_env_id`，再决定后续是否调用 `load_table_env`。

请求示例：

```json
{
  "request_id": "req-009",
  "command_type": "list_table_env",
  "payload": {}
}
```

成功响应示例：

```json
{
  "request_id": "req-009",
  "ok": true,
  "payload": {
    "table_envs": [
      {
        "id": "default"
      },
      {
        "id": "ycb"
      }
    ],
    "table_env_count": 2
  }
}
```

推荐约定：

- `table_envs` 返回当前 `worker` 支持加载的全部桌面环境标识。
- `table_env_count` 返回当前可用桌面环境数量，便于上层快速校验。
- `list_table_env` 只负责枚举支持的环境，不表示这些环境已经被加载。

#### 7.3 `load_table_env`

`load_table_env` 用于让 `worker` 按一个简单的 `table_env_id` 加载一套预定义桌面物体。这里的“桌面物体”只包括桌面上的可操作物体，例如方块、YCB 物体等，不包含地面、光源、摄像头、机械臂和桌子本体。

这里需要明确边界：

- `worker` 启动时已经加载固定基础环境。
- `worker` 自己维护一组硬编码的桌面环境模板，当前版本暂时只有 `default` 和 `ycb` 两套。
- 控制面不再发送复杂对象 JSON，只发送一个简单的 `table_env_id`。
- 旧的 `add_objects` / `add_scene_objects` 控制命令视为已移除，不再作为新协议的一部分继续维护。

当前约定的 `table_env_id`：

- `default`
  由两个动态方块组成，适合最小联调和基础抓取验证。
- `ycb`
  由硬编码 YCB 物体组成，当前优先使用 `/root/Download/YCB/Axis_Aligned_Physics/` 下的可抓取资产；如主机仍保留旧目录，也兼容 `/root/Downloads/YCB/Axis_Aligned_Physics/`。

对于 `ycb` 环境，当前阶段固定遵守以下约束：

- 当前主机上的 YCB 资产目录按 `/root/Download/YCB` 约定；实现里同时兼容历史上的 `/root/Downloads/YCB`。
- 优先使用 `Axis_Aligned_Physics` 版本资产，而不是只带视觉模型的普通版本。
- 推荐通过 Isaac Sim 5.0.0 的 `add_reference_to_stage(usd_path=..., prim_path=...)` 把 USD 引用挂到 stage，再对根 prim 设置位姿、缩放以及刚体/碰撞属性。
- 对动态 YCB 物体，初始 `z` 会放在桌面上方少量高度，让物体在若干个 `world.step(render=True)` 后自然落到桌面并稳定。

请求示例：

```json
{
  "request_id": "req-010",
  "command_type": "load_table_env",
  "payload": {
    "table_env_id": "ycb"
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-010",
  "ok": true,
  "payload": {
    "table_env": {
      "id": "ycb",
      "status": "loaded"
    },
    "objects": [
      {
        "id": "cracker_box_1"
      },
      {
        "id": "mustard_bottle_1"
      }
    ],
    "object_count": 2
  }
}
```

推荐约定：

- 如果 `payload.table_env_id` 不是受支持的值，返回 `ok: false`。
- 如果已经加载过某套桌面环境，再请求不同的 `table_env_id`，返回 `ok: false`。
- 如果重复请求当前已经加载的 `table_env_id`，可以直接返回当前对象状态，按幂等处理。
- `load_table_env` 不负责初始化固定基础环境；固定基础环境在 `worker` 启动时已经加载完毕。
- 响应中应回显当前 `table_env.id`、对象 `id` 和对象数量。

#### 7.4 `get_table_env_objects_info`

`get_table_env_objects_info` 用于查询当前 `worker` 中已经加载的桌面环境对象变换信息。当前协议只返回 `table_env` 相关物体的最新世界坐标位姿和缩放；如果后续需要，也可以扩展为按对象 `id` 过滤。

当前返回体的重点是“当前桌面环境下的对象状态”；其中会回显 `table_env` 状态，方便 API 层确认当前加载的是哪套预定义环境。

请求示例：

```json
{
  "request_id": "req-020",
  "command_type": "get_table_env_objects_info",
  "payload": {}
}
```

成功响应示例：

```json
{
  "request_id": "req-020",
  "ok": true,
  "payload": {
    "table_env": {
      "loaded": true,
      "id": "ycb"
    },
    "object_count": 2,
    "objects": [
      {
        "id": "cracker_box_1",
        "pose": {
          "position_xyz_m": [0.201, 0.179, 1.551],
          "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
        },
        "scale_xyz": [1.0, 1.0, 1.0]
      },
      {
        "id": "mustard_bottle_1",
        "pose": {
          "position_xyz_m": [0.319, 0.181, 1.551],
          "quaternion_wxyz": [0.9998, 0.0, 0.0, 0.0175]
        },
        "scale_xyz": [1.0, 1.0, 1.0]
      }
    ]
  }
}
```

这个响应基本是把当前桌面对象的最新状态重新结构化返回，主要目的是让 `SimManager` 和 API 层拿到当前可展示、可决策的对象信息。

这里需要特别强调：

- 对 `physics.mode = "dynamic"` 的 `usd_asset`，返回的 `pose` 应理解为“当前世界位姿”，而不是最初请求里的生成位姿。
- 这对可抓取 YCB 物体尤其重要：物体在落桌、碰撞稳定后，`pose` 可能与请求中的初始值略有偏差；如果上层是基于 simworker 当前场景做抓取规划，应优先使用这里返回的最新位姿。
- `get_table_env_objects_info` 的返回值和 `run_task` 的执行输入是两件独立的事。
- `run_task` 执行时只使用外部传入的 `task.objects` 快照；即使 `worker` 内部场景里存在对象实时状态，也不会自动读取、合并或替换到任务代码看到的 `objects` 中。

#### 7.5 `get_robot_status`

`get_robot_status` 用于返回当前机器人是否空闲或忙碌，以及它是否正在执行某个任务。

请求示例：

```json
{
  "request_id": "req-030",
  "command_type": "get_robot_status",
  "payload": {
    "robot": {}
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-030",
  "ok": true,
  "payload": {
    "robot": {
      "status": "idle",
      "current_task_id": null
    }
  }
}
```

推荐约定：

- `robot.status` 使用 `idle` 和 `busy` 两种状态。

#### 7.6 `list_camera`

`list_camera` 用于返回当前 `worker` 可用的全部相机 `id`，供 API 层先发现可查询的相机，再按需调用 `get_camera_info` 或后续的视频流命令。

请求示例：

```json
{
  "request_id": "req-040",
  "command_type": "list_camera",
  "payload": {}
}
```

成功响应示例：

```json
{
  "request_id": "req-040",
  "ok": true,
  "payload": {
    "cameras": [
      {
        "id": "table_overview"
      },
      {
        "id": "table_top"
      }
    ],
    "camera_count": 2
  }
}
```

推荐约定：

- `cameras` 返回当前 `worker` 已创建并可访问的全部相机 `id`。
- `camera_count` 返回当前可用相机数量，便于上层快速校验。
- `list_camera` 只负责枚举相机，不返回图片、depth 或流信息。

#### 7.7 `get_camera_info`

`get_camera_info` 用于获取某个相机的当前快照、内参、位姿和 artifact 引用信息。根据第 3 节约束，控制面只返回引用，不直接携带图片和深度内容本体。

请求示例：

```json
{
  "request_id": "req-040",
  "command_type": "get_camera_info",
  "payload": {
    "camera": {
      "id": "table_top"
    }
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-040",
  "ok": true,
  "payload": {
    "camera": {
      "id": "table_top",
      "status": "ready",
      "prim_path": "/World/Cameras/TableTopCamera",
      "mount_mode": "world",
      "resolution": [640, 640],
      "intrinsics": {
        "fx": 533.33,
        "fy": 533.33,
        "cx": 320.0,
        "cy": 320.0,
        "width": 640,
        "height": 640
      },
      "pose": {
        "position_xyz_m": [0.0, 0.0, 6.0],
        "quaternion_wxyz": [0.7071, 0.0, 0.7071, 0.0]
      },
      "rgb_image": {
        "ref": {
          "id": "artifact-rgb-001",
          "kind": "artifact_file",
          "path": "/session_dir/2026-04-01_10-11-12/artifacts/table_top_rgb_10-11-12.png",
          "content_type": "image/png"
        }
      },
      "depth_image": {
        "unit": "meter",
        "ref": {
          "id": "artifact-depth-001",
          "kind": "artifact_file",
          "path": "/session_dir/2026-04-01_10-11-12/artifacts/table_top_depth_10-11-12.npy",
          "content_type": "application/x-npy"
        }
      }
    }
  }
}
```

推荐约定：

- `camera.id` 不存在时，返回 `ok: false`。
- 当前默认同时返回 RGB 和 depth 两种 artifact 引用；如果后续需要，可再扩展成按需返回。

#### 7.8 `start_camera_stream`

`start_camera_stream` 用于通知 `worker` 为某个相机启动一条持续更新的内部视频流。根据第 3.2 节约束，这里返回的是内部流引用信息和元数据，而不是对外 `MJPEG` / `WebRTC` 地址。

请求示例：

```json
{
  "request_id": "req-050",
  "command_type": "start_camera_stream",
  "payload": {
    "camera": {
      "id": "table_top"
    },
    "stream": {
      "buffer_mode": "latest_frame"
    }
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-050",
  "ok": true,
  "payload": {
    "camera": {
      "id": "table_top"
    },
    "stream": {
      "id": "stream-table_top-001",
      "status": "running",
      "buffer_mode": "latest_frame",
      "pixel_format": "rgb24",
      "resolution": [640, 640],
      "ref": {
        "id": "stream-ref-table_top-001",
        "kind": "shared_memory",
        "path": "shm://table_top_main",
        "layout": "latest_frame_v1"
      }
    }
  }
}
```

推荐约定：

- 对同一个相机重复调用 `start_camera_stream` 时，推荐直接返回现有运行中流的信息，使该命令具备幂等性。
- `stream.ref` 只表示内部数据面引用，具体底层字段可在后续单独细化。

当前版本实现约束补充如下：

- 当前只实现 `buffer_mode = latest_frame`。
- 当前只实现 `pixel_format = rgb24`，不提供 depth stream。
- 当前同一时刻每个相机最多维护一条 stream。
- 当前整个系统只有一个 `Worker` 进程。
- 当前由这个唯一 `Worker` 进程的主循环统一推进控制命令处理、`world.step(render=True)` 和 stream 刷新；当主循环空闲且到达刷新周期时，才会刷新全部活跃 stream。
- 当前不为 stream 额外创建第二个 `Worker` 进程，也不再依赖并行后台执行路径去调用 Isaac Sim API。
- 当前 `stream.ref.path` 的实际格式为 `shm://<shared_memory_name>`，供 API 层后续打开共享内存对象使用。
- `SimManager` 在这里的职责到返回 `stream.ref` 为止，不负责 shared memory 打开、帧读取、像素解码或任何 reader 封装。

#### 7.9 `stop_camera_stream`

`stop_camera_stream` 用于停止一条已经启动的内部相机流。

请求示例：

```json
{
  "request_id": "req-060",
  "command_type": "stop_camera_stream",
  "payload": {
    "stream": {
      "id": "stream-table_top-001"
    }
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-060",
  "ok": true,
  "payload": {
    "stream": {
      "id": "stream-table_top-001",
      "status": "stopped"
    }
  }
}
```

推荐约定：

- `stream.id` 不存在时，返回 `ok: false`。
- 停止后，相关内部流资源由 `worker` 负责清理。

#### 7.10 `run_task`

`run_task` 用于执行一个任务。按照第 4 节约束，`worker` 在执行任务期间会阻塞控制面直到任务完成或失败，因此这个命令的响应会在任务结束后返回。

这里执行一个任务，当前协议约定直接向 `worker` 发送一段任务代码以及一份对象快照，由 `worker` 在预置执行上下文中直接执行 `run(robot, objects)`。

对 `run_task` 有一个特殊约定：

- `task.code` 是任务代码字符串。
- `task.objects` 是任务执行输入，结构保持为 `list[dict]`。
- 任务代码中必须提供统一入口函数 `run(robot, objects)`。
- `worker` 会提前准备好预置的高层 `robot` 原子动作 API，再把 `robot` 和请求中的原始 `task.objects` 一起传给 `run(robot, objects)`。
- `run_task` 执行时只认请求中的 `task.objects`；任务代码看到的对象快照就是请求里传入的内容。
- `worker` 不会在执行前去查询 `get_table_env_objects_info`，也不会把内部实时对象状态合并、覆盖或替换到 `task.objects` 上。
- `robot` API 的实现必须与 `worker` 的唯一主循环协作，保证任务执行期间视频流继续推帧。
- `task.code` 被视为完全受信的 Python 代码；当前设计直接执行，不额外做沙箱隔离。
- `run(robot, objects)` 不定义协议层返回值；成功响应里的 `task.result` 当前固定为 `null`。
- 当前设计暂不定义任务超时；只要 `run(robot, objects)` 还没有结束，控制面就继续等待。
- 这不影响 API 层做外层请求超时兜底；如果 `SimManager` 等待单次请求超过设定时限，应直接终止当前 `worker` 进程，而不是再发送一条 `shutdown` 命令，因为此时控制面仍被前一个请求阻塞。

请求示例：

```json
{
  "request_id": "req-070",
  "command_type": "run_task",
  "payload": {
    "task": {
      "id": "task-001",
      "objects": [
        {
          "id": "red_cube",
          "pose": {
            "position_xyz_m": [
              0.16000044345855713,
              0.22000035643577576,
              1.5399998426437378
            ],
            "quaternion_wxyz": [
              1.0,
              -1.1649311772998772e-06,
              1.4712445590703283e-06,
              1.5434716837958717e-09
            ]
          },
          "scale_xyz": [
            0.07999999821186066,
            0.07999999821186066,
            0.07999999821186066
          ]
        },
        {
          "id": "blue_cube",
          "pose": {
            "position_xyz_m": [
              0.3200005292892456,
              -0.1399995982646942,
              1.5399998426437378
            ],
            "quaternion_wxyz": [
              1.0,
              -1.4223320476958179e-06,
              1.5484991990888375e-06,
              9.343960272190088e-08
            ]
          },
          "scale_xyz": [
            0.07999999821186066,
            0.07999999821186066,
            0.07999999821186066
          ]
        }
      ],
      "code": "def run(robot, objects):\\n    red_cube = next(obj for obj in objects if obj['id'] == 'red_cube')\\n    blue_cube = next(obj for obj in objects if obj['id'] == 'blue_cube')\\n    target_center_z = (\\n        blue_cube['pose']['position_xyz_m'][2]\\n        + (blue_cube['scale_xyz'][2] / 2)\\n        + (red_cube['scale_xyz'][2] / 2)\\n        + 0.03\\n    )\\n\\n    robot.pick_and_place(\\n        pick_position=red_cube['pose']['position_xyz_m'],\\n        place_position=[\\n            blue_cube['pose']['position_xyz_m'][0],\\n            blue_cube['pose']['position_xyz_m'][1],\\n            target_center_z,\\n        ],\\n        rotation=None,\\n    )\\n"
    }
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-070",
  "ok": true,
  "payload": {
    "task": {
      "id": "task-001",
      "status": "succeeded",
      "result": null,
      "started_at": "2026-04-01T10:00:00Z",
      "finished_at": "2026-04-01T10:00:12Z"
    }
  }
}
```

失败响应示例：

```json
{
  "request_id": "req-070",
  "ok": false,
  "error_message": "Task failed: inverse kinematics did not converge",
  "payload": {
    "task": {
      "id": "task-001",
      "status": "failed",
      "result": null
    }
  }
}
```

推荐约定：

- 如果当前基础环境尚未初始化，返回 `ok: false`。
- 如果 `task.id`、`task.objects` 或 `task.code` 缺失，返回 `ok: false`。
- 如果 `task.code` 不能编译，或没有提供 `run(robot, objects)`，返回 `ok: false`。
- `task.objects` 在协议层保持 `list[dict]` 结构；任务代码自己负责按 `id` 查找需要的对象。
- `task.objects` 是外部任务输入快照；`run_task` 的执行语义不依赖 `worker` 内部对象信息接口，也不要求调用方事先先调一次 `get_table_env_objects_info`。
- 当前同一时刻只允许执行一个任务。
- `run_task` 的失败响应只返回 `error_message`，不再细分额外 `error_code` 字段。
- 当前设计暂不支持任务超时控制。
- 当前设计不支持 `cancel_task`。
- 任务执行期间，控制面阻塞，但已经启动的视频流继续更新。
- 任务执行期间，不额外提供流控制特例；如果某路流要停止，只能等当前任务返回后由控制面继续处理。

#### 7.11 `shutdown`

`shutdown` 用于关闭当前 `worker`。在阻塞式任务模型下，如果当前正有长任务执行，`shutdown` 只能在该任务返回后被处理。

请求示例：

```json
{
  "request_id": "req-080",
  "command_type": "shutdown",
  "payload": {
    "worker": {}
  }
}
```

成功响应示例：

```json
{
  "request_id": "req-080",
  "ok": true,
  "payload": {
    "worker": {
      "status": "shutting_down"
    }
  }
}
```

推荐约定：

- `worker` 返回成功响应后，应尽快关闭控制 socket 并退出进程。
- 如果需要记录关闭原因，可以在后续为 `worker.metadata` 增加可选字段。

### 8. 推荐的整体边界

为了让系统职责清晰，建议采用以下边界：

- `worker`
  负责 Isaac Sim 初始化、仿真执行、桌面对象管理、相机采集、任务推进和原始视频帧生产。
- `SimManager`
  负责进程拉起、socket 连接、命令编排、启动/关闭/请求级超时兜底、session 生命周期管理和对上层 API 提供统一抽象；其中请求超时后的兜底方式是直接结束 `worker` 进程，而不是再补发 `shutdown`。
- API 服务
  负责 HTTP 对外接口、鉴权、前端可见地址生成和最终响应聚合。

在这个边界下：

- 控制面问题由 `SimManager <-> worker` 协议解决。
- 视频与 artifact 问题由独立数据面解决。
- 日志问题由日志系统解决。

这样可以避免把所有职责都压到一条 `JSON line stdout` 通道里。
