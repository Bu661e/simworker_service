## SimManager API 文档

这份文档只描述当前推荐的对外使用方式。

对外部系统，尤其是 API 层来说，整个 `simworker` 包唯一应该直接依赖的对象是：

```python
from simworker import SimManager, SimManagerError
```

不要直接依赖这些内部模块：

- `runtime.py`
- `handlers.py`
- `protocol.py`
- `entrypoint.py`
- `camera_streams.py`
- `table_environments/*`
- `base_environments/*`
- `robots/*`

这些都属于 `simworker` 内部实现。API 层应该只调用 `SimManager`，把自己保持成一层很薄的业务编排层。

### 1. 推荐使用方式

当前推荐方式如下：

- 外部系统只维护一个长期存活的 `SimManager` 实例。
- 这个 `SimManager` 对应一个唯一的 Sim Worker 进程。
- 创建完 `SimManager` 对象后，推荐尽早显式调用一次 `start()` 或 `ensure_started()`。
- API 层只传业务参数。
- 如果此前还没启动 worker，那么首次调用任一“会向 worker 发送命令”的业务方法时，`SimManager` 也会自动启动 worker；但这属于兜底行为，不是首选接入方式。
- 退出时调用 `shutdown()` 或 `close()` 做资源回收。

一句话说，API 层不需要知道 worker 的控制协议长什么样，也不需要知道 worker 子进程如何启动、如何探活、如何超时回收；这些都已经收在 `SimManager` 内部。

### 2. 快速开始

```python
from simworker import SimManager, SimManagerError

sim_manager = SimManager(
    control_socket_path="/tmp/simworker/control.sock",
    python_bin="/root/isaacsim/python.sh",
)

try:
    sim_manager.ensure_started()
    sim_manager.hello()

    table_envs = sim_manager.list_table_env()
    camera_list = sim_manager.list_camera()
    robot_api_text = sim_manager.list_api()

    sim_manager.load_table_env("default")
    objects_payload = sim_manager.get_table_env_objects_info()
    # 如需切换到另一套桌面环境，先 clear 再 load。
    # sim_manager.clear_table_env()
    # sim_manager.load_table_env("multi_geometry")
    # sim_manager.load_table_env("ycb")

    top_camera_info = sim_manager.get_camera_info("table_top")
    stream_payload = sim_manager.start_camera_stream("table_top")

    task_code = """
def run(robot, objects):
    red_cube = next(obj for obj in objects if obj["id"] == "red_cube")
    blue_cube = next(obj for obj in objects if obj["id"] == "blue_cube")
    target_center_z = (
        blue_cube["pose"]["position_xyz_m"][2]
        + (blue_cube["bbox_size_xyz_m"][2] / 2)
        + (red_cube["bbox_size_xyz_m"][2] / 2)
        + 0.03
    )

    robot.pick_and_place(
        pick_position=red_cube["pose"]["position_xyz_m"],
        place_position=[
            blue_cube["pose"]["position_xyz_m"][0],
            blue_cube["pose"]["position_xyz_m"][1],
            target_center_z,
        ],
        rotation=None,
        grasp_offset=None,
    )
"""

    task_result = sim_manager.run_task(
        task_id="task-001",
        objects=objects_payload["objects"],
        code=task_code,
    )

    sim_manager.stop_camera_stream(stream_payload["stream"]["id"])
    sim_manager.shutdown()
except SimManagerError as exc:
    print(f"command failed: {exc}")
    raise
finally:
    sim_manager.close()
```

上面这个例子故意没有传 `session_dir`，表示直接使用默认值。当前默认目录是仓库内的 `simworker/runs/`。

### 3. 构造函数

`SimManager` 的构造函数如下：

```python
SimManager(
    *,
    session_dir: str | Path | None = None,
    control_socket_path: str | Path,
    python_bin: str = sys.executable,
    worker_module: str = "simworker.entrypoint",
    cwd: str | Path | None = None,
    startup_timeout_sec: float = 240.0,
    request_timeout_sec: float = 180.0,
    shutdown_timeout_sec: float = 60.0,
    extra_env: Mapping[str, str] | None = None,
)
```

参数说明：

- `session_dir`
  可选参数。`SimManager` 会话根目录。不传时默认使用仓库内的 `simworker/runs/`。这里会保存 `simworker.log`，worker 自己的每次运行也会在这里生成独立 `run_dir`。

- `control_socket_path`
  必填。worker 控制面使用的 UDS 路径。API 层不需要自己操作这个 socket，但必须提供一个路径给 `SimManager` 使用。

- `python_bin`
  用来启动 worker 子进程的 Python 解释器。实际接 Isaac Sim 时，推荐传 `/root/isaacsim/python.sh`。

- `worker_module`
  worker 启动模块。默认是 `simworker.entrypoint`。通常不需要改。

- `cwd`
  worker 子进程工作目录。默认是仓库根目录。通常不需要改。

- `startup_timeout_sec`
  启动 worker 后，等待控制 socket 就绪和 `hello` ready probe 成功的最长时间。

- `request_timeout_sec`
  单次命令的最长等待时间。超时后 `SimManager` 会终止 worker，并抛出 `SimManagerError`。

- `shutdown_timeout_sec`
  关闭 worker 时等待其退出的最长时间。

- `extra_env`
  额外注入给 worker 子进程的环境变量。

常用实例属性：

- `session_dir`
  解析后的 `Path` 对象。

- `control_socket_path`
  解析后的 `Path` 对象。

- `process_log_path`
  `session_dir / "simworker.log"`。这是 worker 子进程 stdout / stderr 的汇总日志文件。

### 4. 返回值与异常约定

`SimManager` 会把底层控制协议的外层 JSON 屏蔽掉。

也就是说，worker 原始响应虽然长这样：

```json
{
  "request_id": "req-000001-hello",
  "ok": true,
  "payload": {
    "...": "..."
  }
}
```

但 `SimManager` 对外返回的只是内部 `payload`。

返回值约定：

- `start()` / `ensure_started()` 返回 `SimManager` 自身。
- `is_running()` 返回 `bool`。
- `list_api()` 返回 `str`。
- `close()` 返回 `None`。
- 其他业务方法返回 `dict[str, Any]`，内容就是 worker 成功响应的 `payload`。

异常约定：

- 参数不合法时，`SimManager` 会直接抛 `ValueError`。
- worker 启动失败、控制 socket 异常、worker 返回失败响应、响应超时、响应格式不合法时，`SimManager` 会抛 `SimManagerError`。
- `SimManagerError` 附带以下信息，便于 API 层记录日志：
  - `request_id`
  - `command_type`
  - `payload`

### 5. 同步 / 异步语义

当前 `SimManager` 对外公开的方法全部是同步接口。

这意味着：

- 当前没有 `async def` 风格接口。
- 当前没有回调式接口。
- 调用方进入某个方法后，会一直阻塞到该次调用完成、失败，或者超时抛错。

需要特别区分的只有两类：

- `start_camera_stream()` 本身是同步调用；但它返回后，视频帧会继续由 worker 主循环异步写入共享内存。
- `run_task()` 不是“提交任务后立即返回”的接口；它会同步阻塞到任务执行完成或失败返回。

### 6. 生命周期方法

#### `start() -> SimManager`

用途：
显式启动 worker，并等待 worker ready。

参数：
无。

返回值：

```python
self
```

调用语义：
同步。会阻塞到 worker ready，或者启动失败抛错。

说明：

- 如果 worker 已经在运行，会先做一次 `hello` 健康检查，然后直接返回。
- 当前推荐用法是：创建 `SimManager` 后尽早显式调一次 `start()`。
- 其他业务方法虽然也能自动拉起 worker，但更适合作为兜底，而不是主流程。

#### `ensure_started() -> SimManager`

用途：
语义上等同于“确保 worker 已启动”。

参数：
无。

返回值：

```python
self
```

调用语义：
同步。会阻塞到 worker ready，或者启动失败抛错。

说明：

- 当前实现等价于 `start()`。
- 如果你更喜欢“幂等地确保已启动”这个语义，可以在 API 层统一调用它。

#### `is_running() -> bool`

用途：
查询当前 worker 是否可用。

参数：
无。

返回值：

```python
True | False
```

调用语义：
同步。本地检查当前状态，不会向 worker 发送命令。

说明：

- 这个方法会综合检查子进程状态和控制 socket 是否可连接。

#### `shutdown() -> dict[str, Any]`

用途：
向 worker 发送正常关闭指令，并等待其退出。

参数：
无。

返回值：

worker 正常运行时：

```python
{
    "worker": {
        "status": "shutting_down",
    }
}
```

worker 本来就没在运行时：

```python
{
    "worker": {
        "status": "stopped",
    }
}
```

调用语义：
同步。会阻塞到 worker 退出完成，或者确认 worker 本来就未运行。

说明：

- 这是“显式关闭并拿到关闭响应”的方法。
- 如果你需要一个明确的关闭结果，调用它。

#### `close() -> None`

用途：
做 best-effort 资源回收。

参数：
无。

返回值：

```python
None
```

调用语义：
同步。会阻塞到 best-effort 清理完成。

说明：

- 如果 worker 还活着，`close()` 会尽量先发 `shutdown`。
- 如果 `shutdown` 失败，`close()` 仍会继续做最终清理，不再把关闭阶段的异常继续向外抛。
- 适合写在 `finally` 块或上下文管理器退出路径里。

### 7. 基础查询方法

#### `hello() -> dict[str, Any]`

用途：
探活，并返回 worker 当前总体状态。

参数：
无。

返回值：

```python
{
    "worker": {
        "status": "ready",
    },
    "table_env": {
        "loaded": False,
        "id": None,
    },
    "objects": {
        "object_count": 0,
    },
    "robot": {
        "status": "idle",
        "current_task_id": None,
    },
    "streams": {
        "active_count": 0,
    },
}
```

调用语义：
同步。会阻塞到本次状态查询完成。

说明：

- 这是最直接的 ready 检查方法。
- 如果此前还没启动 worker，`hello()` 也会自动拉起 worker。
- 但当前更推荐先显式调用 `ensure_started()` 或 `start()`，再把 `hello()` 当成状态查询接口使用。

#### `list_table_env() -> dict[str, Any]`

用途：
列出当前 worker 支持的桌面环境 ID。

参数：
无。

返回值：

```python
{
    "table_envs": [
        {"id": "default"},
        {"id": "multi_geometry"},
        {"id": "ycb"},
    ],
    "table_env_count": 3,
}
```

调用语义：
同步。会阻塞到本次查询完成。

说明：

- 当前实现内置 `default`、`multi_geometry` 和 `ycb` 三个桌面环境。
- API 层应优先以该接口的返回结果为准，而不是在外部写死。

#### `list_api() -> str`

用途：
返回当前 robot 可用 API 的说明文本。

参数：
无。

返回值：

```text
当前 robot 可用 API 如下：
...
```

调用语义：
同步。会阻塞到文本查询完成。

说明：

- 当前返回的是纯文本字符串，不是结构化 JSON。
- 文本内容来自 `simworker/robots/api_reference.txt`。

#### `list_camera() -> dict[str, Any]`

用途：
列出当前 worker 中所有可用摄像头 ID。

参数：
无。

返回值：

```python
{
    "cameras": [
        {"id": "table_overview"},
        {"id": "table_top"},
    ],
    "camera_count": 2,
}
```

调用语义：
同步。会阻塞到本次查询完成。

说明：

- 当前默认基础环境里有两个摄像头：`table_overview` 和 `table_top`。
- 具体相机内参、位姿和拍照产物请再调用 `get_camera_info()`。

#### `get_robot_status() -> dict[str, Any]`

用途：
查询机械臂状态。

参数：
无。

返回值：

```python
{
    "robot": {
        "status": "idle",
        "current_task_id": None,
    }
}
```

调用语义：
同步。会阻塞到本次查询完成。

说明：

- `status` 当前主要取值是 `idle` 或 `busy`。
- `current_task_id` 表示当前正在执行的任务；空闲时为 `None`。

### 8. 桌面环境与相机方法

#### `load_table_env(table_env_id: str) -> dict[str, Any]`

用途：
加载一个硬编码桌面环境。

参数：

- `table_env_id`
  桌面环境 ID，必须是非空字符串。当前可通过 `list_table_env()` 查询。

返回值：

```python
{
    "table_env": {
        "id": "default",
        "status": "loaded",
    },
    "objects": [
        {"id": "red_cube"},
        {"id": "blue_cube"},
    ],
    "object_count": 2,
}
```

调用语义：
同步。会阻塞到桌面环境加载完成，或返回当前已加载环境，或抛错。

说明：

- 这个接口只负责加载预设环境，不接收复杂对象 JSON。
- 同一时刻最多只允许存在一套已加载的 `table_env`。
- 如果当前还没加载环境，请求会真正执行加载。
- 如果已经加载了同一个 `table_env_id`，会直接返回当前已加载环境。
- 如果已经加载了另一个 `table_env_id`，会报错；调用方应先执行 `clear_table_env()`，再加载新的环境。
- `multi_geometry` 当前会返回 8 个对象，其中包含 2 个固定分类圆盘 `left_plate` / `right_plate`、3 个立方体和 3 个圆柱体。
- 当前基础场景按机器人视角约定 `front = +y`、`back = -y`、`left = -x`、`right = +x`、`up = +z`；因此 `left_plate` 在 `x < 0`，`right_plate` 在 `x > 0`。

#### `clear_table_env() -> dict[str, Any]`

用途：
清空当前已经加载的桌面环境物体，为后续重新加载另一套 `table_env` 做准备。

参数：
无。

返回值：

```python
{
    "table_env": {
        "loaded": False,
        "id": None,
        "status": "cleared",
    },
    "previous_table_env_id": "default",
    "object_count": 0,
    "objects": [],
}
```

调用语义：
同步。会阻塞到当前桌面环境对象被清空，或返回当前已经为空的状态。

说明：

- 这个接口只清空当前 `table_env` 加载出来的桌面物体。
- 它不会删除基础环境里的桌子、机械臂、相机、地面、灯光，也不会主动停止已有视频流。
- 如果当前没有已加载环境，会按幂等方式返回 `table_env.status = "empty"`，同时 `previous_table_env_id = None`。
- 当前实现默认由调用方保证机器人空闲时再调用，也就是不要在 `run_task()` 执行过程中调用它。

#### `get_table_env_objects_info() -> dict[str, Any]`

用途：
获取当前桌面环境内所有物体的实时位姿、包围盒尺寸、几何描述和颜色信息。

参数：
无。

返回值：

```python
{
    "table_env": {
        "loaded": True,
        "id": "default",
    },
    "object_count": 2,
    "objects": [
        {
            "id": "red_cube",
            "pose": {
                "position_xyz_m": [0.2, 0.0, 1.55],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.06, 0.06, 0.06],
            "geometry": {
                "type": "cuboid",
                "size_xyz_m": [0.06, 0.06, 0.06],
            },
            "color": [1.0, 0.0, 0.0],
        },
        {
            "id": "blue_cube",
            "pose": {
                "position_xyz_m": [0.3, 0.0, 1.55],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.06, 0.06, 0.06],
            "geometry": {
                "type": "cuboid",
                "size_xyz_m": [0.06, 0.06, 0.06],
            },
            "color": [0.0, 0.0, 1.0],
        },
    ],
}
```

例如，在 `multi_geometry` 环境下，返回体里不同类型对象的 `objects` 条目可长这样
（下面只节选盘子、立方体、圆柱体 3 个代表对象；完整返回时 `object_count` 仍为 `8`）：

```python
{
    "table_env": {
        "loaded": True,
        "id": "multi_geometry",
    },
    "object_count": 8,
    "objects": [
        {
            "id": "left_plate",
            "pose": {
                "position_xyz_m": [-0.5, 0.01, 1.5075],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.4, 0.4, 0.015],
            "geometry": {
                "type": "cylinder",
                "radius_m": 0.2,
                "height_m": 0.015,
            },
            "color": [0.15, 0.75, 0.85],
        },
        {
            "id": "red_cube",
            "pose": {
                "position_xyz_m": [-0.14, 0.12, 1.57],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.08, 0.08, 0.08],
            "geometry": {
                "type": "cuboid",
                "size_xyz_m": [0.08, 0.08, 0.08],
            },
            "color": [1.0, 0.0, 0.0],
        },
        {
            "id": "yellow_cube",
            "pose": {
                "position_xyz_m": [0.0, 0.12, 1.57],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.08, 0.08, 0.08],
            "geometry": {
                "type": "cuboid",
                "size_xyz_m": [0.08, 0.08, 0.08],
            },
            "color": [1.0, 1.0, 0.0],
        },
        {
            "id": "blue_cylinder",
            "pose": {
                "position_xyz_m": [0.14, -0.1, 1.575],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            },
            "bbox_size_xyz_m": [0.08, 0.08, 0.09],
            "geometry": {
                "type": "cylinder",
                "radius_m": 0.04,
                "height_m": 0.09,
            },
            "color": [0.0, 0.0, 1.0],
        },
    ],
}
```

调用语义：
同步。会阻塞到本次查询完成。

说明：

- `pose.position_xyz_m` 和 `pose.quaternion_wxyz` 都是 world 坐标系下的数据。
- 这些数据是在查询时直接从 Isaac Sim 物体 handle 读取的，因此会反映物体当前真实状态。
- 这个接口适合给上层做拍照后感知结果核对，也适合把对象信息传给 LLM。
- `multi_geometry` 当前返回的 `objects` 会包含 2 个固定分类圆盘 `left_plate` / `right_plate`，以及红/黄/蓝三色的 3 个立方体和 3 个圆柱体；如果上层只需要可抓取物体，应自行过滤掉盘子。
- `bbox_size_xyz_m` 表示对象局部坐标系下的包围盒尺寸，统一用米为单位。
- `geometry` 用于补充对象形状参数；例如长方体返回 `type = "cuboid"` 和 `size_xyz_m`，圆柱体返回 `type = "cylinder"`、`radius_m`、`height_m`。
- 对不规则真实物体，`geometry.type` 可为 `mesh`，同时通过 `bbox_size_xyz_m` 提供统一尺寸描述。
- `color` 当前约定为 RGB 三元组；如果对象本身没有稳定的单一颜色语义，也可以返回 `None`。

#### `get_camera_info(camera_id: str) -> dict[str, Any]`

用途：
拍摄当前相机的 RGB 图和深度图，同时返回相机元数据。

参数：

- `camera_id`
  相机 ID，必须是非空字符串。可先通过 `list_camera()` 获取。

返回值：

```python
{
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
            "height": 640,
        },
        "pose": {
            "position_xyz_m": [0.0, 0.0, 6.0],
            "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5],
        },
        "rgb_image": {
            "ref": {
                "id": "artifact-rgb-001",
                "kind": "artifact_file",
                "path": "/abs/path/to/rgb.png",
                "content_type": "image/png",
            }
        },
        "depth_image": {
            "unit": "meter",
            "ref": {
                "id": "artifact-depth-001",
                "kind": "artifact_file",
                "path": "/abs/path/to/depth.npy",
                "content_type": "application/x-npy",
            }
        },
    }
}
```

调用语义：
同步。会阻塞到本次拍照、深度图导出和元数据查询全部完成。

说明：

- 每次调用都会产生新的 RGB / depth 产物文件，保存在当前 worker run 目录下的 `artifacts/` 中。
- `pose` 是相机当前 world pose。
- `mount_mode` 当前实现里可能是 `world` 或 `usd`。

#### `start_camera_stream(camera_id: str, buffer_mode: str = "latest_frame") -> dict[str, Any]`

用途：
启动指定相机的视频流。

参数：

- `camera_id`
  相机 ID，必须是非空字符串。

- `buffer_mode`
  缓冲模式，必须是非空字符串。当前实现只支持 `latest_frame`。

返回值：

```python
{
    "camera": {
        "id": "table_top",
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
            "path": "shm://stream_table_top_001_xxxxxxxx",
            "layout": "latest_frame_v1",
        },
    },
}
```

调用语义：
同步。会阻塞到视频流创建完成并拿到共享内存引用信息；方法返回后，帧会继续由 worker 主循环持续写入共享内存。

说明：

- `SimManager` 只负责启动视频流并返回共享内存引用信息。
- `SimManager` 不负责读帧；共享内存的读取逻辑应该由 API 层或上层流模块自己实现。
- 当前像素格式是 `rgb24`。
- 如果同一个相机已经有运行中的流，再次调用会直接返回当前流信息，而不是再开一条新流。

#### `stop_camera_stream(stream_id: str) -> dict[str, Any]`

用途：
停止一条视频流。

参数：

- `stream_id`
  视频流 ID，必须是非空字符串。通常来自 `start_camera_stream()` 的返回值。

返回值：

```python
{
    "stream": {
        "id": "stream-table_top-001",
        "status": "stopped",
    }
}
```

调用语义：
同步。会阻塞到流停止并完成 worker 侧资源回收。

说明：

- 停止后，共享内存资源会在 worker 内被回收。

### 9. 任务方法

#### `run_task(*, task_id: str, objects: list[dict[str, Any]], code: str) -> dict[str, Any]`

用途：
执行一段由外部生成的 Python 任务代码。

参数：

- `task_id`
  任务 ID，必须是非空字符串。

- `objects`
  一个 `list[dict]`。这是调用方提供的外部对象快照。

- `code`
  一段 Python 源码字符串，必须定义：

```python
def run(robot, objects):
    ...
```

返回值：

```python
{
    "task": {
        "id": "task-001",
        "status": "succeeded",
        "result": None,
        "started_at": "2026-04-03T12:34:56.000000+00:00",
        "finished_at": "2026-04-03T12:35:08.000000+00:00",
    }
}
```

调用语义：
同步。会阻塞到任务执行完成并返回结果，或者失败抛错；它不是异步提交接口。

`code` 的推荐形态如下：

```python
def run(robot, objects):
    red_cube = next(obj for obj in objects if obj["id"] == "red_cube")
    blue_cube = next(obj for obj in objects if obj["id"] == "blue_cube")
    target_center_z = (
        blue_cube["pose"]["position_xyz_m"][2]
        + (blue_cube["bbox_size_xyz_m"][2] / 2)
        + (red_cube["bbox_size_xyz_m"][2] / 2)
        + 0.03
    )

    robot.pick_and_place(
        pick_position=red_cube["pose"]["position_xyz_m"],
        place_position=[
            blue_cube["pose"]["position_xyz_m"][0],
            blue_cube["pose"]["position_xyz_m"][1],
            target_center_z,
        ],
        rotation=None,
        grasp_offset=None,
    )
```

说明：

- `objects` 是外部传入的快照；worker 在执行 `run(robot, objects)` 时，会把这份 `objects` 原样传进去，不会用内部场景数据替换它。
- 当前协议不消费 `run()` 的返回值，所以成功响应里的 `task.result` 固定是 `None`。
- 同一时刻只允许一个任务执行；如果已有任务在运行，再发新任务会失败。
- `robot` 参数是 `simworker` 预先准备好的机械臂动作 API 对象；当前可用 API 可通过 `list_api()` 查询。
- 任务执行期间，robot 动作会和 worker 的唯一主循环协作推进世界，因此已有视频流不会因为任务执行而停掉。

### 10. 推荐调用顺序

对于 API 层，当前推荐的调用顺序通常是：

1. 创建一个长期存活的 `SimManager`
2. `ensure_started()` 或 `start()`
3. `hello()`
4. 对于不同HTTP请求，根据需要调用：
    - `list_table_env()` / `list_camera()` / `list_api()`
    - `load_table_env(table_env_id)`
    - `clear_table_env()`
    - `get_table_env_objects_info()`
    - `get_camera_info(camera_id)` 
    - `start_camera_stream(camera_id)`
    - `run_task(...)`
5. `stop_camera_stream(stream_id)`
6. `shutdown()` 或 `close()`

其中：

- 如果整个外部系统只使用一个 worker，那么 `load_table_env()` 通常在系统启动后的早期阶段就应该完成；后续如需切换环境，推荐走 `clear_table_env()` 再 `load_table_env()`。
- 如果 API 层需要给 LLM 提供对象快照和 robot API 文本，推荐分别使用 `get_table_env_objects_info()` 和 `list_api()`。
- 如果 API 层需要拍照确认环境是否真的加载成功，推荐在 `load_table_env()` 之后立即调用 `get_camera_info()`。

### 11. 一句话总结

对外部系统来说，`SimManager` 就是整个 `simworker`。

API 层只需要给它传业务参数，拿业务结果；worker 生命周期、进程管理、控制协议、超时回收、任务执行和流协作，都应该留在 `SimManager` 和 `simworker` 内部。
