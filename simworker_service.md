# simworker_service

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
- 如果只是需要切换桌面环境，优先在原 worker 上清空当前 `table_env`，而不是直接重启 worker

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
- 除“单帧采集接口”、“采集产物下载接口”和“视频流接口”外，其余接口尽量直接映射 `SimManager`，不在 API 层重复发明一套状态机和协议
- 除 `GET /cameras/{camera_id}/stream` 外，其余接口都采用同步请求-响应模式
- `run_task` 也是同步接口：HTTP 请求会阻塞到任务执行完成或失败返回
- 除下面两个成功响应例外外，其余接口响应体统一使用 JSON：
  - `GET /captures/{capture_id}/artifacts/{artifact_kind}` 成功时返回文件本体
  - `GET /cameras/{camera_id}/stream` 成功时返回视频流本体
- API 层所有接口统一返回 HTTP `200 OK`
- JSON 响应统一约定：
  - 成功时：`ok=true`，并带上业务字段
  - 失败时：`ok=false`，并用 `error_message` 表达错误原因
- API 层尽量直接沿用对应 `SimManager` 方法返回的 payload，只在最外层补 `ok`
- 这三个特殊接口的处理原则如下：
  - 单帧采集接口：
    - API 层先调用 `SimManager.get_camera_info(camera_id)`
    - 再把返回中的本地 artifact 路径改写成对外可下载的 `download_url`
    - 并补充 `capture.id`、`capture.created_at`
  - 采集产物下载接口：
    - `POST /cameras/{camera_id}/capture` 返回的 `download_url` 就是指向这个接口的具体地址
    - API 层根据 `capture.id + artifact_kind` 定位对应 artifact 文件
    - 成功时直接返回文件本体，不额外包 `ok`
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

### 3. 采集单帧 RGBD 元数据与下载链接

#### 3.1 采集单帧

`POST /cameras/{camera_id}/capture`

接口名称：采集单帧 RGBD 元数据

接口作用：

- 触发指定摄像头采集一帧 RGB 和 Depth
- 返回本次结果对应的 JSON 元数据
- JSON 中保留相机分辨率、内参、位姿等基础信息
- RGB 图像和 Depth 图像不再直接跟随接口返回，而是分别提供下载链接
- 该接口是 API 层的一个特例：它会先调用 `SimManager.get_camera_info(camera_id)`，再把结果中的本地 artifact 路径改写成下载链接

成功响应：

- HTTP `200 OK`
- `Content-Type: application/json`

响应体示例：

```json
{
  "ok": true,
  "capture": {
    "id": "capture-4d0d5b1f9d6b4f17ab9c6c7d95f0a4e1",
    "created_at": "2026-04-03T04:00:00Z"
  },
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
        "content_type": "image/png",
        "download_url": "/captures/capture-4d0d5b1f9d6b4f17ab9c6c7d95f0a4e1/artifacts/rgb"
      }
    },
    "depth_image": {
      "unit": "meter",
      "ref": {
        "id": "artifact-depth-001",
        "kind": "artifact_file",
        "content_type": "application/x-npy",
        "download_url": "/captures/capture-4d0d5b1f9d6b4f17ab9c6c7d95f0a4e1/artifacts/depth"
      }
    }
  }
}
```

说明：

- `capture.id` 是 API 层生成的本次采集记录标识，用于后续下载接口。
- `download_url` 是 API 层对外暴露的 artifact 相对下载路径，不直接暴露 worker 本地文件路径。
- 客户端拿到 `download_url` 后，直接发起一次 `GET` 请求即可下载对应文件；不需要再做别的转换。
- `pose` 仍然保留，方便兼容上层已有调用方。
- 当前下载链接由 API 进程内存中的 `capture.id -> artifact` 映射维护；如果 API 服务重启，旧链接不保证继续可用。

前端推荐使用方式：

1. 前端调用 `POST /cameras/{camera_id}/capture`，拿到本次采集的 JSON 响应。
2. 前端从响应中直接读取：
   - `camera.resolution`
   - `camera.intrinsics`
   - `camera.pose`
   - `camera.rgb_image.ref.download_url`
   - `camera.depth_image.ref.download_url`
3. 如果前端当前只需要元数据用于展示、标注或传给上层逻辑，可以只消费 JSON，不立即下载图片文件。
4. 如果前端需要显示 RGB 图像或拿到深度文件，则直接 `GET` 对应的 `download_url`。
5. 前端通常不需要自己拼接 `capture.id` 或 `artifact_kind`，直接使用 3.1 返回的 URL 即可。

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "camera.id table_top_xxx does not exist"
}
```

#### 3.2 下载单个采集产物

`GET /captures/{capture_id}/artifacts/{artifact_kind}`

接口名称：下载单个采集产物

接口作用：

- 下载某次 `capture` 产生的单个文件型产物
- `POST /cameras/{camera_id}/capture` 响应里的 `download_url` 就是这个接口的相对路径
- 这一节主要是说明 `download_url` 最终落到哪个 API 路径，以及该路径返回什么内容
- 当前 `artifact_kind` 只支持：
  - `rgb`
  - `depth`
- 该接口是 API 层的数据面接口，不再经过 `SimManager`

调用建议：

- 外部调用方通常不需要手工拼接或单独记忆这个接口路径。
- 对外推荐使用方式仍然是：先调用 3.1，再直接请求 3.1 返回的 `download_url`。
- 只有在调试、排查问题、或明确知道 `capture_id` 与 `artifact_kind` 的情况下，才需要显式关注这个路径模板。

成功响应：

- HTTP `200 OK`
- `artifact_kind = rgb` 时，`Content-Type: image/png`
- `artifact_kind = depth` 时，`Content-Type: application/x-npy`
- 建议响应头包含：
  - `Content-Disposition: attachment; filename="table_top_rgb_04-00-00-000001.png"`

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "capture.id capture-missing does not exist"
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
  - 将每帧编码为 JPEG
  - 通过 HTTP `MJPEG` 对外发布
  - 在客户端断开后，通过 `SimManager.stop_camera_stream(stream_id)` 回收底层流

当前状态：

- 当前对外接口路径先固定为 `GET /cameras/{camera_id}/stream`
- 当前实现已经固定为 `MJPEG`
- 这是一个面向“单客户端、自用、非生产环境”的最小实现
- 当前不做多客户端复用，不做订阅计数，不做复杂重连
- 底层统一依赖：
  - `SimManager.start_camera_stream(camera_id)`
  - `SimManager.stop_camera_stream(stream_id)`

实现方式：

- API 层从共享内存读取 `rgb24` 帧
- 把每帧编码成 JPEG
- 通过 HTTP 流直接返回给前端
- 成功响应头固定为：
  - `Content-Type: multipart/x-mixed-replace; boundary=frame`
- 每个 part 携带一帧 JPEG 图像

优点：

- 代码最简单
- 最容易调试
- 最适合当前阶段快速跑通

缺点：

- 带宽明显高于压缩视频方案
- 延迟通常高于 WebRTC
- 当前实现只按单客户端场景做最小闭环，不提供更复杂的流管理能力

后续扩展说明：

- 如果后面确认需要更低延迟、更低带宽的视频预览，可以再单独新增 `WebRTC` 方案
- 但这不属于当前已实现内容

成功响应：

- HTTP `200 OK`
- `Content-Type: multipart/x-mixed-replace; boundary=frame`

说明：

- 该接口对客户端来说是单个 HTTP 视频流接口
- 对 API 层内部来说，这一版只需要“收到请求就启动一条底层流，断开时回收”
- 当前不额外维护多客户端订阅计数

前端接入方式：

- 由于当前返回的是标准 `MJPEG`，前端最简单的接法就是直接把该 URL 放进浏览器 `<img>` 标签的 `src`
- 例如：

```html
<img
  src="http://<your-host>:8000/cameras/table_top/stream"
  alt="table_top stream"
  style="max-width: 100%; height: auto;"
/>
```

- 如果前端是 React，也同样直接写：

```tsx
export function CameraStream() {
  return (
    <img
      src="http://<your-host>:8000/cameras/table_top/stream"
      alt="table_top stream"
      style={{ maxWidth: "100%", height: "auto" }}
    />
  );
}
```

- 前端不需要自己解析 `multipart/x-mixed-replace`
- 浏览器会持续接收并刷新 JPEG 帧
- 如果后端返回的是 `ok=false` 的 JSON 错误，那么这个 `<img>` 不会正常显示，因此前端通常还应先调用一次 `GET /cameras` 或在页面上额外做错误兜底

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
- 当前不额外返回 `display_name`、预览图或其他展示层字段
- 当前内置环境包括 `default`、`multi_geometry` 和 `ycb`

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
      "id": "multi_geometry"
    },
    {
      "id": "ycb"
    }
  ],
  "table_env_count": 3
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
- 同一时刻最多只允许存在一套已加载的 `table_env`
- 当前内置环境 ID 以 `GET /table-envs` 返回结果为准；当前实现包括 `default`、`multi_geometry` 和 `ycb`
- `multi_geometry` 当前包含 8 个对象，其中有 2 个固定分类圆盘 `left_plate` / `right_plate`

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
- 如果已经加载了另一个 `table_env_id`，则返回 `ok=false`，调用方应先调用 `DELETE /table-env/current`

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "table_env_id ycb does not match current loaded table_env_id default"
}
```

### 7. 清空当前桌面环境

`DELETE /table-env/current`

接口名称：清空当前桌面环境

接口作用：

- 清空当前 worker 中由 `table_env` 加载出来的桌面物体
- 该接口直接映射 `SimManager.clear_table_env()`
- 只影响当前桌面环境对象，不删除桌子、机械臂、相机、地面、灯光等基础环境
- 该接口用于“切换到另一套桌面环境前先清空当前环境”

成功响应：

- HTTP `200 OK`

响应体示例：

```json
{
  "ok": true,
  "table_env": {
    "loaded": false,
    "id": null,
    "status": "cleared"
  },
  "previous_table_env_id": "default",
  "object_count": 0,
  "objects": []
}
```

状态约定：

- 如果当前存在已加载环境，则清空成功后返回 `table_env.loaded=false`
- 如果当前本来就没有已加载环境，也按幂等方式返回成功；此时 `table_env.status="empty"`、`previous_table_env_id=null`
- 前端如需切换环境，推荐调用顺序是：
  - `DELETE /table-env/current`
  - `PUT /table-env/current/{table_env_id}`

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```

### 8. 获取当前桌面环境中的物体信息

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
      "bbox_size_xyz_m": [0.06, 0.06, 0.06],
      "geometry": {
        "type": "cuboid",
        "size_xyz_m": [0.06, 0.06, 0.06]
      },
      "color": [1.0, 0.0, 0.0]
    },
    {
      "id": "blue_cube",
      "pose": {
        "position_xyz_m": [0.3, 0.0, 1.55],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "bbox_size_xyz_m": [0.06, 0.06, 0.06],
      "geometry": {
        "type": "cuboid",
        "size_xyz_m": [0.06, 0.06, 0.06]
      },
      "color": [0.0, 0.0, 1.0]
    }
  ]
}
```

例如，在 `multi_geometry` 环境下，返回体里的 `objects` 可以节选成下面这样
（下面只保留盘子、立方体、长方体、圆柱体 4 类代表对象；真实返回时 `object_count` 仍为 `8`）：

```json
{
  "ok": true,
  "table_env": {
    "loaded": true,
    "id": "multi_geometry"
  },
  "object_count": 8,
  "objects": [
    {
      "id": "left_plate",
      "pose": {
        "position_xyz_m": [-0.34, 0.01, 1.5075],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "bbox_size_xyz_m": [0.18, 0.18, 0.015],
      "geometry": {
        "type": "cylinder",
        "radius_m": 0.09,
        "height_m": 0.015
      },
      "color": [0.15, 0.75, 0.85]
    },
    {
      "id": "red_cube",
      "pose": {
        "position_xyz_m": [-0.14, 0.12, 1.57],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "bbox_size_xyz_m": [0.08, 0.08, 0.08],
      "geometry": {
        "type": "cuboid",
        "size_xyz_m": [0.08, 0.08, 0.08]
      },
      "color": [1.0, 0.0, 0.0]
    },
    {
      "id": "green_block",
      "pose": {
        "position_xyz_m": [0.14, 0.12, 1.56],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "bbox_size_xyz_m": [0.12, 0.08, 0.06],
      "geometry": {
        "type": "cuboid",
        "size_xyz_m": [0.12, 0.08, 0.06]
      },
      "color": [0.0, 1.0, 0.0]
    },
    {
      "id": "purple_cylinder",
      "pose": {
        "position_xyz_m": [0.0, -0.1, 1.575],
        "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
      },
      "bbox_size_xyz_m": [0.08, 0.08, 0.09],
      "geometry": {
        "type": "cylinder",
        "radius_m": 0.04,
        "height_m": 0.09
      },
      "color": [0.6, 0.0, 0.8]
    }
  ]
}
```

字段说明：

- `pose.position_xyz_m`：物体 world 坐标系下的位置
- `pose.quaternion_wxyz`：物体 world 坐标系下的朝向
- `bbox_size_xyz_m`：物体局部坐标系下的包围盒尺寸
- `geometry`：物体几何描述；规则几何体会返回对应参数，不规则物体可返回 `type = "mesh"`
- `color`：对象颜色，当前约定为 RGB 三元组；如果没有稳定的单一颜色，也可以返回 `null`
- 当前基础场景按机器人视角约定 `front = +y`、`back = -y`、`left = -x`、`right = +x`、`up = +z`
- 对 `multi_geometry` 而言，`left_plate` 位于 `x < 0`，`right_plate` 位于 `x > 0`
- `multi_geometry` 的 `objects` 当前会返回 8 个对象，包含这两个固定分类圆盘；如果前端或任务代码只关心可抓取物体，需要自行过滤
- `bbox_size_xyz_m` 是统一尺寸描述字段；前端或上层逻辑可以优先基于它做尺寸展示或粗粒度规划
- 如果需要更精确的形状参数，例如圆柱体半径和高度，应从 `geometry` 里读取

失败响应体示例：

```json
{
  "ok": false,
  "error_message": "worker unavailable"
}
```


### 9. 获取机械臂状态

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

### 10. 获取机械臂 API 说明

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

### 11. 执行机械臂任务

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
        "bbox_size_xyz_m": [0.06, 0.06, 0.06],
        "geometry": {
          "type": "cuboid",
          "size_xyz_m": [0.06, 0.06, 0.06]
        },
        "color": [1.0, 0.0, 0.0]
      },
      {
        "id": "blue_cube",
        "pose": {
          "position_xyz_m": [0.3, 0.0, 1.55],
          "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
        },
        "bbox_size_xyz_m": [0.06, 0.06, 0.06],
        "geometry": {
          "type": "cuboid",
          "size_xyz_m": [0.06, 0.06, 0.06]
        },
        "color": [0.0, 0.0, 1.0]
      }
    ],
    "code": "def run(robot, objects):\n    red_cube = next(obj for obj in objects if obj[\"id\"] == \"red_cube\")\n    blue_cube = next(obj for obj in objects if obj[\"id\"] == \"blue_cube\")\n    target_center_z = (\n        blue_cube[\"pose\"][\"position_xyz_m\"][2]\n        + (blue_cube[\"bbox_size_xyz_m\"][2] / 2)\n        + (red_cube[\"bbox_size_xyz_m\"][2] / 2)\n        + 0.03\n    )\n\n    robot.pick_and_place(\n        pick_position=red_cube[\"pose\"][\"position_xyz_m\"],\n        place_position=[\n            blue_cube[\"pose\"][\"position_xyz_m\"][0],\n            blue_cube[\"pose\"][\"position_xyz_m\"][1],\n            target_center_z,\n        ],\n        rotation=None,\n        grasp_offset=None,\n    )\n"
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

### 12. 手动重启 Worker

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
9. 如果需要切换到另一套环境，先调用 `DELETE /table-env/current`，再调用新的 `PUT /table-env/current/{table_env_id}`。
10. 客户端按需调用 `POST /cameras/{camera_id}/capture`，获取本次采集的 JSON 元数据、内参与 RGB / Depth 下载链接。
11. 如果前端需要实时预览，则打开 `GET /cameras/{camera_id}/stream`；API 层内部负责启动并在断开时回收底层 simworker stream。
12. 客户端结合：
    - `GET /table-env/current/objects`
    - `POST /cameras/{camera_id}/capture`
    - `GET /robot/api`
    生成 `run(robot, objects)` 形式的任务代码。
13. 客户端调用 `POST /robot/tasks`，提交任务对象并同步等待执行结果。
14. 执行期间或执行完成后，客户端可以调用 `GET /health` 或 `GET /robot/status` 查看状态。
15. 如果需要重新回到干净状态，客户端也可以调用 `POST /worker/restart`；成功返回后，新 worker 仍只有基础环境，没有自动恢复任何 `table_env`。
