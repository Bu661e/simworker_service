# Frontend Backend Request Flow

## 目标

记录当前前端页面与两个后端之间的请求时序约定，作为后续 mock 实现、真实联调和接口检查的统一参考。

## 后端地址

### IsaacSim Service

- Base URL: `https://vsq4t8n3-wteq1vxp-18080.ahrestapi.gpufree.cn:8443`

### Grasp Planner Service

- Base URL: `http://localhost:20000`

## 开发代理

当前前端在本地 `real` 联调模式下，不直接让浏览器跨域请求后端，而是先走 Vite 开发代理。

- IsaacSim 请求前缀：`/api/sim`
- Grasp Planner 请求前缀：`/api/planner`

说明：

- 浏览器在开发环境下实际请求的是同源地址：
  - `GET /api/sim/table-envs`
  - `POST /api/planner/planning/code`
- Vite 再把这些请求转发到真实后端
- 这样做的目的是规避浏览器 CORS 限制
- 文档下面写的 `{isaacSimBaseUrl}`、`{graspPlannerBaseUrl}` 仍然表示逻辑上的后端目标地址

## 固定约定

### 视频流联调说明

这一节专门说明当前 `MJPEG` 视频流联调中已经暴露出来的问题、后端已经做的修复，以及前端仍然必须遵守的约定。

### 当前问题是什么

- `/cameras/{camera_id}/stream` 是一个长连接 `HTTP MJPEG` 接口，不是普通一次性请求
- 前端如果在同一页面里重复挂载同一个 `<img src=".../stream">`，就会为同一相机发出多条并发流请求
- 这种重复请求通常来自：
  - 页面重渲染时反复改动 `<img>` 的 `key` 或 `src`
  - 路由切换时旧页面还没完全卸载，新页面已经开始接流
  - React 开发模式下的 `StrictMode` 双挂载
- SimWorker 对同一个相机会复用同一条底层 stream，因此多个前端请求可能实际共享同一个底层 `stream_id`
- 之前 API 层在每个 HTTP 请求结束时都会直接 stop 底层 stream，于是会出现重复 stop 同一个 `stream_id` 的情况，典型日志就是 `stream.id ... does not exist`

### 后端已经做了什么修改

- API 层现在已经对 `MJPEG` 请求增加了按 `stream_id` 的 consumer 引用计数
- 同一个 `camera_id` 的多个 HTTP 请求如果复用了同一条底层 stream，API 层只会在最后一个 consumer 断开时才真正调用 stop
- 如果某条底层 stream 已经被提前关闭，API 层会把这次 stop 当作幂等清理处理，不再把它当成异常错误
- 这次修改只发生在 API 层，没有改 SimWorker 的接口设计

### 这不代表前端可以不改

- 后端现在只是做了兜底，避免重复 stop 把日志打爆
- 前端如果继续重复接流，仍然会带来额外带宽、额外连接数和更复杂的页面生命周期问题
- 所以前端仍然应该把“每个相机只保留一个活跃 consumer”当成正式约定，而不是依赖后端兜底

### 前端还需要改什么

- 同一页面内，同一个 `camera_id` 最多只保留一个活跃 `<img src=".../stream">`
- 视频流组件挂载后不要在普通状态更新里反复修改 `src`
- 视频流组件也不要频繁变更 `key`
- 页面离开、切换会话、点击“结束会话”时，先卸载 `<img>` 或清空其 `src`，确认浏览器开始断开 MJPEG 请求后，再调用 `DELETE /table-env/current`
- 如果前端是 React，开发环境下要额外检查 `StrictMode` 是否导致了重复挂载
- 开发联调时优先走 Vite 代理路径，不要让浏览器直接跨域请求真实后端流地址

### 前端如何理解现在的边界

- 前端不需要新增“显式关闭视频流”的独立后端接口
- 前端拿到 `/cameras/{camera_id}/stream` 后，直接作为 `<img src>` 使用即可
- 流的开启由浏览器发起请求决定
- 流的关闭由浏览器断开请求决定
- 前端真正要控制的是组件生命周期，而不是额外去调一个 stop API

### 环境 ID

- `default`
- `multi_geometry`
- `ycb`

补充说明：

- 当前前端配置里已经包含 `multi_geometry`
- 但第一页下拉框默认先显示 `default` 和 `ycb`
- 只有当 `GET /table-envs` 返回 `multi_geometry` 时，第一页才会把它展示出来

### 相机 ID

- 左侧窗口，俯视视角: `table_top`
- 右侧窗口，斜前视角: `table_overview`

### 四个阶段

1. 获取场景信息
2. 3D 信息获取
3. 执行代码推理
4. 任务执行

第 4 阶段当前只展示三种状态：

- `任务执行中`
- `执行成功`
- `执行失败`

## 请求时序

### 1. 打开第一页

第一页初始化时，前端当前实现只会获取可用环境列表。

当前实现：

```text
GET {isaacSimBaseUrl}/table-envs
```

作用：

- `GET /table-envs`: 获取当前仿真服务支持的环境 ID
- 前端再用这些 ID 过滤本地环境配置，决定下拉框里实际显示哪些环境

### 2. 点击“启动会话”

点击按钮后，前端先请求仿真服务加载当前环境，再进入第二页。第二页加载完成后，当前实现会立即准备视频流和机器人能力信息。

当前实现：

```text
PUT {isaacSimBaseUrl}/table-env/current/{table_env_id}
GET {isaacSimBaseUrl}/cameras
GET {isaacSimBaseUrl}/robot/api
```

页面资源加载：

```text
<img src="{isaacSimBaseUrl}/cameras/table_top/stream">
<img src="{isaacSimBaseUrl}/cameras/table_overview/stream">
```

当前未实现：

```text
GET {isaacSimBaseUrl}/robot/status
```

作用：

- `PUT /table-env/current/{table_env_id}`: 加载当前桌面环境
- `GET /cameras`: 获取当前可用相机 ID
- `GET /robot/api`: 获取机器人动作 API 文本
- `table_top/stream`: 接入左侧俯视视频流
- `table_overview/stream`: 接入右侧斜前方视频流

说明：

- 第二页的视频流不是点击发送任务后才开始接，而是页面打开后立即通过 `<img src>` 接入
- 视频流接口当前是 MJPEG，前端可以直接作为 `<img src="...">` 使用
- `GET /robot/api` 的结果应缓存到前端状态，供后续 `/planning/code` 请求直接复用
- 同一页面内，同一个 `camera_id` 只应保留一个活跃 `<img src=".../stream">`
- 普通状态更新不要反复改动视频流组件的 `key` 或 `src`，避免重复发起同一相机的 `/stream` 请求
- 如果前端处于 React 开发模式，需要注意 StrictMode 可能带来的双挂载行为，避免重复接流

### 3. 点击“结束会话”

当前前端不是单纯返回第一页，而是会先断开页面上的两个 MJPEG 连接，再清空当前桌面环境，最后离开第二页。

请求顺序：

```text
卸载或清空两个 <img src=".../stream">
DELETE {isaacSimBaseUrl}/table-env/current
```

作用：

- 清空当前 `table_env` 加载出来的桌面物体
- 不影响桌子、机械臂、相机、灯光等基础环境
- 成功后前端再返回第一页

当前 UI 约定：

- 如果任务正在执行中，`结束会话` 按钮禁用
- 如果正在执行清空操作，按钮显示 `结束中...`
- 页面离开前应先让两个 `<img>` 断开连接，再触发 `DELETE /table-env/current`
- 只有 `DELETE /table-env/current` 成功后，前端才会跳回第一页
- 如果清空失败，前端停留在当前页并显示错误提示

### 4. 点击“发送指令”

点击发送后，前端按四阶段顺序发请求。

标准顺序：

```text
POST {isaacSimBaseUrl}/cameras/table_overview/capture
GET {isaacSimBaseUrl}/table-env/current/objects
POST {graspPlannerBaseUrl}/planning/code
POST {isaacSimBaseUrl}/robot/tasks
```

#### 第 1 阶段：获取场景信息

```text
POST {isaacSimBaseUrl}/cameras/table_overview/capture
```

作用：

- 从桌面斜前方相机采集一帧 RGB / Depth
- 返回相机内参、位姿、下载链接等元数据
- 如果 `capture` 返回的 artifact `download_url` 是相对路径，前端会先按 IsaacSim Base URL 拼成绝对地址，再传给后续 Planner 请求

关键输出：

- `camera`
- `camera.rgb_image.ref.download_url`
- `camera.depth_image.ref.download_url`

#### 第 2 阶段：3D 信息获取

第 2 阶段在语义上仍然叫“3D 信息获取”，因为前端需要拿到可供规划使用的 `objects` 数组。

这一阶段目前存在两种可选方式。

当前默认方式：

```text
GET {isaacSimBaseUrl}/table-env/current/objects
```

请求体：

- 无

作用：

- 直接从仿真服务读取当前桌面环境中的全部物体信息
- 返回 `objects` 数组，可直接传给后续代码推理与任务执行

关键输出：

- `objects`

备选方式：

```text
POST {graspPlannerBaseUrl}/perception/objects
```

请求体核心结构：

```json
{
  "task": "<自然语言任务>",
  "camera": "<capture 接口返回的 camera 对象>"
}
```

作用：

- 根据任务描述和相机采集结果提取任务相关的 3D 物体信息

关键输出：

- `objects`

当前约定：

- 由于 Planner 的感知接口暂未实现，前端当前默认使用仿真服务的 `GET /table-env/current/objects`
- 等 Planner 感知接口可用后，可以切回 `POST /perception/objects`
- 当前前端支持手动切换这两种方式，开关位置在 [src/config/appConfig.ts](/Users/haitong/Code_ws/robot_grasp_v2/grasp_web/src/config/appConfig.ts) 的 `appRuntimeConfig.objectAcquisitionMode`
- 当开关为 `simulation` 时，前端会让第 2 阶段额外停留一段可配置时间，用来模拟合规的 3D 信息获取耗时
- 当前配置值是 `10000ms`，也就是 10 秒

#### 第 3 阶段：执行代码推理

```text
POST {graspPlannerBaseUrl}/planning/code
```

请求体核心结构：

```json
{
  "task": "<自然语言任务>",
  "robot_api": "<第二页初始化阶段缓存的 robot api 字符串>",
  "objects": [],
  "image": {
    "ref": {
      "content_type": "<capture 接口返回的 RGB content_type>",
      "download_url": "<capture 接口返回的 RGB download_url>"
    }
  }
}
```

作用：

- 根据任务描述、机器人动作 API 和 3D 感知结果生成可执行代码
- 规划阶段还会带上当前抓图得到的 RGB 地址，供 Planner 下载图像并参与多模态推理

补充说明：

- 正常情况下，`robot_api` 在第二页初始化时就已经通过 `GET /robot/api` 获取并缓存
- 如果前端本地缓存缺失，发送任务时可以兜底再请求一次 `GET /robot/api`
- `image.ref.download_url` 和 `image.ref.content_type` 来自第 1 阶段 `capture` 返回的 `camera.rgb_image.ref`
- 如果 `camera.rgb_image.ref.download_url` 是相对路径，前端会先把它拼成 `https://.../captures/...` 形式的绝对地址

关键输出：

- `llm_code`

#### 第 4 阶段：任务执行

```text
POST {isaacSimBaseUrl}/robot/tasks
```

请求体核心结构：

```json
{
  "task": {
    "id": "<task-id>",
    "objects": [],
    "code": "<llm_code>"
  }
}
```

作用：

- 将生成的代码与物体快照提交给仿真服务执行

关键输出：

- `task.status`
- `task.started_at`
- `task.finished_at`

当前 UI 约定：

- 请求发送中时显示 `任务执行中`
- 返回 `ok=true` 时显示 `执行成功`
- 返回 `ok=false` 时显示 `执行失败`

## 页面行为总结

### 第一页

- 获取环境列表
- 选择环境
- 点击启动会话时加载环境
- 会话结束后返回第一页，并保留当前 `env` 选择

### 第二页

- 页面打开立即接入两个视频流
- 页面打开时获取相机列表与机器人 API
- 每个相机在同一页面内最多保持一个活跃视频流 consumer
- 点击结束会话时先断开两个视频流，再调用 `DELETE /table-env/current`
- 发送任务时按四阶段顺序调用后端

## 当前实现建议

在前端实现上，推荐把请求层分成三部分：

1. 页面状态层
2. 接口调用层
3. mock / real service 切换层

这样后续从 mock 切换到真实后端时，只需要替换请求实现，不需要重写页面状态逻辑。
