# SimWorker GUI Task Runner

这个目录不是 `pytest` 自动化测试目录，而是用来做 **Isaac Sim GUI 手工联调** 的。

它的目标是：

- 启动 Isaac Sim 的 GUI 界面
- 按指定参数加载 `base`、`default`、`multi_geometry` 或 `ycb` 场景
- 读取一个 `objects.json` 文件和一个 `code.py` 文件
- 直接复用当前仓库里的 `run_task()` 逻辑执行任务代码

当前入口文件是：

- [run_task_gui.py](/root/simworker_service/simworker/test_gui/run_task_gui.py)
- [launch_task_gui.sh](/root/simworker_service/simworker/scripts/launch_task_gui.sh)

当前目录里也可以放你手工生成的任务输入文件，例如：

- `*.objects.json`
- `*.py`

## 什么时候用这个目录

适合下面这种场景：

- 你想真实打开 Isaac Sim GUI 观察机械臂动作
- 你已经有 `SimManager.run_task()` 需要的 `objects` 参数和 `code` 参数
- 你不想经过 FastAPI，而是直接在本地做仿真联调
- 你想快速切换 `default` / `multi_geometry` / 空基础环境做验证

不适合下面这种场景：

- 纯接口自动化回归测试  
  这类测试请看 [simworker/tests/README.md](/root/simworker_service/simworker/tests/README.md)
- API 层联调  
  这类测试请看 [tests/README.md](/root/simworker_service/tests/README.md)

## 运行前提

运行前需要满足：

- Isaac Sim 已安装，例如 `/root/isaacsim`
- 可以使用 Isaac Sim 自带的 `python.sh`
- 当前机器可以正常打开 Isaac Sim GUI
- 当前仓库路径是 `/root/simworker_service`

默认会使用：

```text
/root/isaacsim/python.sh
```

如果你的 Isaac Sim 不在这个位置，可以通过环境变量覆盖：

```bash
ISAAC_SIM_ROOT=/your/isaacsim/path
```

## 任务文件约定

### `code.py`

代码文件必须定义：

```python
def run(robot, objects):
    ...
```

这里的 `robot` 和正式 `run_task()` 一样，调用的是当前 `simworker` 内部已经暴露的 robot API。  
这里的 `objects` 也和正式 `run_task()` 一样，是一个对象列表。

### `objects.json`

如果你传了 `--objects-file`，它必须是一个 JSON 数组，也就是 `run_task()` 里的 `objects` 列表格式。

例如：

```json
[
  {
    "id": "red_cube",
    "pose": {
      "position_xyz_m": [0.16, 0.22, 1.53],
      "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]
    },
    "bbox_size_xyz_m": [0.06, 0.06, 0.06],
    "geometry": {
      "type": "cuboid",
      "size_xyz_m": [0.06, 0.06, 0.06]
    },
    "color": [0.62, 0.06, 0.06]
  }
]
```

如果你 **不传** `--objects-file`，runner 会在场景加载完成后，直接读取当前场景里的对象信息，并把它作为 `run_task()` 的 `objects` 参数。

## 场景参数

`--table-env` 目前支持：

- `base`
- `empty`
- `none`
- `default`
- `multi`
- `multi_geometry`
- `ycb`

含义如下：

- `base` / `empty` / `none`
  只加载基础环境，也就是桌子、机械臂、相机，不加载桌面物体
- `default`
  加载默认桌面场景
- `multi` / `multi_geometry`
  加载多几何体桌面场景
- `ycb`
  加载 YCB 桌面场景

其中：

- `multi` 是 `multi_geometry` 的简写
- `base`、`empty`、`none` 是同义写法

## 推荐启动命令

### 1. 加载 `default` 场景，并使用你自己保存的 `objects.json` + `code.py`

```bash
ISAAC_SIM_ROOT=/root/isaacsim \
./simworker/scripts/launch_task_gui.sh \
  --table-env default \
  --objects-file simworker/test_gui/objects.json \
  --code-file simworker/test_gui/code.py
```

适用场景：

- 你希望严格复现某一次规划输出
- 你手里已经有固定的 `objects` 文件

注意：

- `objects.json` 最好和当前加载的场景匹配  
  例如 `default` 场景的对象文件不要直接拿去跑 `multi_geometry`

### 2. 加载 `multi_geometry` 场景，但直接使用当前场景对象，不单独传 `objects.json`

```bash
ISAAC_SIM_ROOT=/root/isaacsim \
./simworker/scripts/launch_task_gui.sh \
  --table-env multi \
  --code-file simworker/test_gui/code.py
```

适用场景：

- 你只想复用 `run(robot, objects)` 代码
- 你希望 `objects` 始终来自当前真实加载的场景

这个模式通常更稳，因为不会出现“文件里的对象信息和当前场景不一致”的问题。

### 3. 只打开基础环境，不加载桌面物体

```bash
ISAAC_SIM_ROOT=/root/isaacsim \
./simworker/scripts/launch_task_gui.sh \
  --table-env base \
  --code-file simworker/test_gui/code.py
```

这个模式适合：

- 单独观察机械臂动作
- 排查“是不是接触物体才开始抖”
- 先验证代码是否能正常执行到 robot API

### 4. 执行结束后自动关闭 GUI

默认情况下，任务执行完以后 GUI 会继续保持打开，方便你观察最终状态。  
如果你希望执行完成后自动退出，可以加：

```bash
ISAAC_SIM_ROOT=/root/isaacsim \
./simworker/scripts/launch_task_gui.sh \
  --table-env default \
  --code-file simworker/test_gui/code.py \
  --close-on-complete
```

## 参数说明

`launch_task_gui.sh` 最终调用的是 `run_task_gui.py`，当前主要参数如下：

- `--table-env`
  指定要加载的桌面环境
- `--objects-file`
  可选；指定 `run_task()` 的 `objects` JSON 文件
- `--code-file`
  必填；指定任务代码文件
- `--task-id`
  可选；不传时默认使用 `code-file` 的文件名 stem
- `--session-dir`
  可选；指定本次运行的日志和产物目录
- `--close-on-complete`
  可选；任务完成后自动关闭 GUI

## 日志与产物目录

默认情况下，这个 GUI runner 的运行目录会放在：

```text
/root/simworker_service/simworker/test_gui/runs/
```

每次运行都会创建一个时间戳目录，例如：

```text
/root/simworker_service/simworker/test_gui/runs/2026-04-06_12-00-00/
```

通常你会在里面看到：

- `worker.log`
- `artifacts/`

如果任务代码里触发了拍照或深度图导出，artifact 也会落在这里。

如果你想手动指定输出目录，可以这样：

```bash
ISAAC_SIM_ROOT=/root/isaacsim \
./simworker/scripts/launch_task_gui.sh \
  --session-dir /root/simworker_service/simworker/test_gui/runs/manual_run_001 \
  --table-env default \
  --code-file simworker/test_gui/code.py
```

## 一个常见建议

如果你只是想验证一段任务代码在某个场景里能不能执行，优先推荐：

1. 先不传 `--objects-file`
2. 先让 runner 自己读取当前场景对象
3. 确认代码本身没问题后，再切换成固定的 `objects.json`

这样更不容易因为对象位姿、尺寸、场景不一致导致误判。
