# IsaacSim Service V0

## 目标
这是一个个人自用的最小化服务。
- 服务部署在云服务器上，但默认只有我自己使用
- API 层使用 FastAPI
- IsaacSim 作为独立 worker 进程运行
- API 层通过 `SimManager` 管理这个唯一的 worker
目标不是做通用平台，而是提供一个稳定、简单、方便调试的仿真控制入口。

## 基本原则
- 只支持单用户、单 worker
- 如果 worker 状态异常，选择重启，而不是做复杂恢复
- 如果 worker 已经加载了桌面物体，不在原 worker 上清理，直接报错并重启 worker

- 不考虑多租户，不考虑并发调度
- 不优先追求通用性，优先追求简单可用
- 不上生产，越简单越好
- 不考虑多 worker 管理
- 不考虑多用户隔离
- 不考虑队列调度
- 不考虑复杂任务系统
- 不提供“不断逐个加物体”的能力，而是提供“加载一套桌面场景”的能力。


## 整体结构
服务分为两层：
1. API 层
  - 对外提供 HTTP 接口
2. IsaacSim Worker 进程
  - API层通过 `SimManager` 管理唯一的 IsaacSim worker


## SimManager 使用说明

文件在：simworker/sim_manager_introduction.md


## API 层对外接口

### 接口调用模式

- API 层内部只维护一个长期存活的 `SimManager`
- API 服务启动时，推荐显式调用一次 `SimManager.ensure_started()`
- 除“单帧采集 zip 接口”和“视频流接口”外，其余接口尽量直接映射 `SimManager`，不在 API 层重复发明一套状态机和协议
- 除 `GET /cameras/{camera_id}/stream` 外，其余接口都采用同步请求-响应模式
- `run_task` 也是同步接口：HTTP 请求会阻塞到任务执行完成或失败返回
- 除下面两个成功响应例外外，其余接口响应体统一使用 JSON：
  - `POST /cameras/{camera_id}/capture` 成功时返回 `zip`
  - `GET /cameras/{camera_id}/stream` 成功时返回视频流本体
- API 层所有接口统一返回 HTTP `200 OK`
- JSON 响应统一约定：
  - 成功时：`ok=true`，并带上业务字段
  - 失败时：`ok=false`，并用 `error_message` 表达错误原因
- API 层尽量直接沿用对应 `SimManager` 方法返回的 payload，只在最外层补 `ok`
- 这两个例外接口的处理原则如下：
  - 单帧采集接口：
    - API 层先调用 `SimManager.get_camera_info(camera_id)`
    - 再把返回中的 RGB / Depth 产物与元数据打成一个 zip 返回
  - 视频流接口：
    - API 层需要自己处理共享内存读帧、编码和对外发布
    - 底层相机流的创建与销毁仍然通过 `SimManager.start_camera_stream()` / `SimManager.stop_camera_stream()` 完成
    - 视频流成功响应是视频流本体，不再额外包 `ok`
    - 如果流启动失败，则返回 HTTP `200 OK` 的 JSON 错误响应：`{"ok": false, "error_message": "..."}`

### 1. 健康检查

`GET /health`

接口名称：健康检查

接口作用：

- 判断 API 服务是否存活
- 返回当前 worker、table environment、robot、stream 的实际状态
- 该接口直接映射 `SimManager.hello()`

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
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
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 2. 获取摄像头列表

`GET /cameras`

接口名称：获取摄像头列表

接口作用：

- 返回当前 worker 可用的所有摄像头 ID
- 该接口直接映射 `SimManager.list_camera()`
- API 层不再额外维护一套摄像头注册表

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
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
```

字段说明：

- `id`：摄像头唯一标识，后续用于单帧采集和视频流接口
- 当前基础环境内固定有两个摄像头：`table_overview`、`table_top`

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 3. 采集单帧 RGBD 打包结果

`POST /cameras/{camera_id}/capture`

接口名称：采集单帧 RGBD 打包结果

接口作用：

- 触发指定摄像头采集一帧 RGB 和 Depth
- 将 RGB 编码为 `png`
- 将 Depth 保存为 `npy`，保留原始浮点深度值
- 将本次结果直接打包成一个 `zip` 二进制响应返回给客户端
- 客户端拿到这个打包结果后，可以直接转发给另外一个服务器，或者本地解包使用
- 该接口是 API 层的一个特例：它会先调用 `SimManager.get_camera_info(camera_id)`，再把结果重新打包

成功响应：

- HTTP `200 OK`
- `Content-Type: application/zip`

响应体说明：

- 响应体本身就是一个 zip 文件，不是 JSON
- 建议响应头包含：
  - `Content-Disposition: attachment; filename="table_top_20260403_120000.zip"`

zip 包内文件约定：

- `rgb.png`
- `depth.npy`
- `camera_info.json`

`camera_info.json` 约定：

- 优先直接复用 `SimManager.get_camera_info(camera_id)` 的成功 payload
- 也就是说，它的结构尽量保持为：

```json
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
      "height": 640
    },
    "pose": {
      "position_xyz_m": [0.0, 0.0, 6.0],
      "quaternion_wxyz": [0.5, 0.5, 0.5, 0.5]
    },
    "rgb_image": {
      "ref": {
        "id": "artifact-rgb-001",
        "kind": "artifact_file",
        "path": "/root/simworker_service/simworker/runs/.../artifacts/table_top_rgb_xxx.png",
        "content_type": "image/png"
      }
    },
    "depth_image": {
      "unit": "meter",
      "ref": {
        "id": "artifact-depth-001",
        "kind": "artifact_file",
        "path": "/root/simworker_service/simworker/runs/.../artifacts/table_top_depth_xxx.npy",
        "content_type": "application/x-npy"
      }
    }
  }
}
```

说明：

- `camera_info.json` 中当前允许保留 `SimManager` 原始返回里的本地 artifact 路径
- 调用方真正需要直接使用的文件仍然是 zip 中的 `rgb.png` 和 `depth.npy`
- 这样可以最大程度复用 `SimManager` 已有 JSON 结构，减少 API 层重新组织字段的成本

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "camera.id table_top_xxx does not exist"
}
```

### 4. 视频流接口

`GET /cameras/{camera_id}/stream`

接口名称：获取摄像头实时视频流

接口作用：

- 为前端提供指定摄像头的实时预览入口
- 这是 API 层唯一不适合直接透传 `SimManager` payload 的接口
- API 层内部需要：
  - 通过 `SimManager.start_camera_stream(camera_id)` 启动底层相机流
  - 从共享内存读取 `rgb24` 帧
  - 自己做编码与对外发布
  - 在最后一个客户端断开后，通过 `SimManager.stop_camera_stream(stream_id)` 回收底层流

当前状态：

- 当前对外接口路径先固定为 `GET /cameras/{camera_id}/stream`
- 但内部究竟使用 `MJPEG` 还是 `WebRTC`，目前还没有最终决定
- 两种方案都允许，它们都依赖同一个底层入口：
  - `SimManager.start_camera_stream(camera_id)`
  - `SimManager.stop_camera_stream(stream_id)`

实现方案 A：MJPEG

- API 层从共享内存读取 `rgb24` 帧
- 把每帧编码成 JPEG
- 通过 HTTP 流直接返回给前端
- 响应头可使用：
  - `Content-Type: multipart/x-mixed-replace; boundary=frame`
- 每个 part 携带一帧 JPEG 图像

优点：

- 代码最简单
- 最容易调试
- 最适合 V0 阶段快速跑通

缺点：

- 带宽明显更高
- 延迟通常高于 WebRTC
- 后续如果要做更正式的实时视频系统，可扩展性一般

实现方案 B：WebRTC

- API 层从共享内存读取 `rgb24` 帧
- 把帧送入 Python 侧 WebRTC 视频轨道
- 前端通过浏览器原生 WebRTC 能力播放
- API 层需要额外提供最小 signaling 能力

当前对 WebRTC 的约束明确如下：

- 只考虑一个客户端
- 只考虑我自己使用
- 不是生产环境
- 不追求工业级稳定性
- 不做多客户端复用
- 不做复杂重连
- 不做复杂监控
- 优先做一个能跨公网在本地浏览器里看到画面的最小 demo
- 可以直接使用公共 STUN
- 当前不预设 TURN

优点：

- 带宽更低
- 延迟更低
- 更接近正式视频流方案

缺点：

- 实现复杂度明显高于 MJPEG
- 需要处理 offer / answer / ICE 等最小 WebRTC 流程
- 即便是最小 demo，也比 MJPEG 多一层 signaling 和媒体轨道封装

成功响应：

- 如果内部实现选的是 MJPEG：
  - HTTP `200 OK`
  - `Content-Type: multipart/x-mixed-replace; boundary=frame`
- 如果内部实现选的是 WebRTC：
  - 该路径可以继续保留为占位入口
  - 实际 signaling 可由 API 层另行定义，例如单独增加 `POST /webrtc/offer`

说明：

- 该接口对客户端来说是单个 HTTP 视频流接口
- 但对 API 层内部来说，需要维护 `camera_id -> 底层 stream_id` 的映射和订阅计数
- 当前这一节只锁定三件事：
  - 外部预览能力一定存在
  - 底层一定依赖 `SimManager` 的相机流接口
  - `MJPEG` 和 `WebRTC` 都是可选实现，但当前尚未最终拍板

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "camera.id table_top_xxx does not exist"
}
```


### 5. 获取桌面环境列表

`GET /table-envs`

接口名称：获取桌面环境列表

接口作用：

- 返回当前服务支持的所有 `table_env_id`
- 该接口直接映射 `SimManager.list_table_env()`
- V0 中不额外返回 `display_name`、预览图或其他展示层字段

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
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
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 6. 加载桌面环境

`PUT /table-env/current/{table_env_id}`

接口名称：加载桌面环境

接口作用：

- 根据 `table_env_id` 加载一套预设桌面环境
- 该接口直接映射 `SimManager.load_table_env(table_env_id)`
- API 层不再自己维护“清空场景后再加载”的逻辑
- 当前 worker 生命周期内只允许加载一个 `table_env`

URL 示例：

```text
PUT /table-env/current/default
```

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
  "table_env": {
    "id": "default",
    "status": "loaded"
  },
  "objects": [
    {
      "id": "red_cube"
    },
    {
      "id": "blue_cube"
    }
  ],
  "object_count": 2
}
```

状态约定：

- 如果当前还没加载任何环境，则会实际执行加载
- 如果已经加载了同一个 `table_env_id`，则直接返回当前环境信息
- 如果已经加载了另一个 `table_env_id`，则返回 `ok=false`

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "table_env_id ycb does not match current loaded table_env_id default"
}
```

### 7. 获取当前桌面环境中的物体信息

`GET /table-env/current/objects`

接口名称：获取当前桌面环境中的物体信息

接口作用：

- 返回当前桌面环境中的所有物体信息
- 该接口直接映射 `SimManager.get_table_env_objects_info()`
- 返回结果可直接传给上层感知模块或 LLM

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
  "table_env": {
    "loaded": true,
    "id": "default"
  },
  "object_count": 2,
  "objects": [
    {
      "id": "red_cube",
      "pose": {
        "position_xyz_m": [0.2, 0.0, 1.55],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "scale_xyz": [0.06, 0.06, 0.06]
    },
    {
      "id": "blue_cube",
      "pose": {
        "position_xyz_m": [0.3, 0.0, 1.55],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "scale_xyz": [0.06, 0.06, 0.06]
    }
  ]
}
```

字段说明：

- `pose.position_xyz_m`：物体 world 坐标系下的位置
- `pose.quaternion_wxyz`：物体 world 坐标系下的朝向
- `scale_xyz`：物体 world scale

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```


### 8. 获取机械臂状态

`GET /robot/status`

接口名称：获取机械臂状态

接口作用：

- 返回当前机械臂状态
- 该接口直接映射 `SimManager.get_robot_status()`

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
  "robot": {
    "status": "idle",
    "current_task_id": null
  }
}
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 9. 获取机械臂 API 说明

`GET /robot/api`

接口名称：获取机械臂 API 说明

接口作用：

- 返回当前 robot 可用动作 API 的完整文本说明
- 该接口直接映射 `SimManager.list_api()`
- 该文本可以直接提供给 LLM 作为动作 API 文档

成功响应：

- HTTP `200 OK`
- `Content-Type: application/json`

响应体示例：

```json
{
  "ok": true,
  "api": "当前 robot 可用 API 如下：\n\n1. pick_and_place(\n       pick_position: list[float],\n       place_position: list[float],\n       rotation: list[float] | None = None,\n       grasp_offset: list[float] | None = None,\n   ) -> None\n..."
}
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 10. 执行机械臂任务

`POST /robot/tasks`

接口名称：执行机械臂任务

接口作用：

- API 层接收一个任务对象，并将其转交给 `SimManager.run_task(...)`
- 请求体中的 `objects` 是外部传入的快照；API 层应原样透传，不要替换成内部场景查询结果
- 任务代码必须定义：
  - `def run(robot, objects): ...`
- 该接口是同步接口，不是异步任务提交接口

请求体示例：

- `Content-Type: application/json`

```json
{
  "task": {
    "id": "task-001",
    "objects": [
      {
        "id": "red_cube",
        "pose": {
          "position_xyz_m": [0.2, 0.0, 1.55],
          "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
        },
        "scale_xyz": [0.06, 0.06, 0.06]
      },
      {
        "id": "blue_cube",
        "pose": {
          "position_xyz_m": [0.3, 0.0, 1.55],
          "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
        },
        "scale_xyz": [0.06, 0.06, 0.06]
      }
    ],
    "code": "def run(robot, objects):\n    red_cube = next(obj for obj in objects if obj[\"id\"] == \"red_cube\")\n    blue_cube = next(obj for obj in objects if obj[\"id\"] == \"blue_cube\")\n    target_center_z = (\n        blue_cube[\"pose\"][\"position_xyz_m\"][2]\n        + (blue_cube[\"scale_xyz\"][2] / 2)\n        + (red_cube[\"scale_xyz\"][2] / 2)\n        + 0.03\n    )\n\n    robot.pick_and_place(\n        pick_position=red_cube[\"pose\"][\"position_xyz_m\"],\n        place_position=[\n            blue_cube[\"pose\"][\"position_xyz_m\"][0],\n            blue_cube[\"pose\"][\"position_xyz_m\"][1],\n            target_center_z,\n        ],\n        rotation=None,\n        grasp_offset=None,\n    )\n"
  }
}
```

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
  "task": {
    "id": "task-001",
    "status": "succeeded",
    "result": null,
    "started_at": "2026-04-03T12:34:56.000000+00:00",
    "finished_at": "2026-04-03T12:35:08.000000+00:00"
  }
}
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "another task is already running: task-000"
}
```

### 11. 手动重启 Worker

`POST /worker/restart`

接口名称：手动重启 Worker

接口作用：

- 手动关闭当前 worker 并重新启动一个新的 worker
- API 层内部可通过关闭旧 `SimManager` 并重新 `ensure_started()` 实现
- 重启成功后，不自动恢复此前已加载的 `table_env`
- 重启成功后，返回新 worker 的最新健康状态

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
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
```

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker restart failed"
}
```

## 建议的数据流

一个更完整的典型调用流程如下：

1. 服务启动。
2. API 层创建唯一的 `SimManager`，并显式调用一次 `ensure_started()`。
3. 客户端调用 `GET /health`，确认 worker 已处于 `ready`。
4. 客户端调用 `GET /table-envs`，获取当前可选的 `table_env_id`。
5. 客户端调用 `GET /cameras`，获取当前可用的 `camera_id`。
6. 客户端调用 `GET /robot/api`，获取当前可用机械臂动作 API 文本。
7. 客户端调用 `PUT /table-env/current/{table_env_id}`，加载目标桌面环境。
8. 客户端调用 `GET /table-env/current/objects`，获取当前桌面环境中的所有物体信息。
9. 客户端按需调用 `POST /cameras/{camera_id}/capture`，获取一个包含 `rgb.png`、`depth.npy`、`camera_info.json` 的 zip 包。
10. 如果前端需要实时预览，则打开 `GET /cameras/{camera_id}/stream`；API 层内部负责启动 / 复用 / 回收底层 simworker stream。
11. 客户端结合：
    - `GET /table-env/current/objects`
    - `POST /cameras/{camera_id}/capture`
    - `GET /robot/api`
    生成 `run(robot, objects)` 形式的任务代码。
12. 客户端调用 `POST /robot/tasks`，提交任务对象并同步等待执行结果。
13. 执行期间或执行完成后，客户端可以调用 `GET /health` 或 `GET /robot/status` 查看状态。
14. 如果需要重新回到干净状态，客户端可调用 `POST /worker/restart`；成功返回后，新 worker 仍只有基础环境，没有自动恢复任何 `table_env`。
