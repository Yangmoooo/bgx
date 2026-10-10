# bgx

把非交互命令安静地放到后台，关闭终端后仍能在另一个终端按名称找到并停止它。

```sh
bgx start web -- python3 -m http.server 8000
bgx list
bgx log web
bgx stop web
bgx remove web
```

## 安装

需要 Make 和 [.zig-version](.zig-version) 指定的 Zig；macOS 还需要 Command Line Tools。

```sh
make
make install PREFIX="$HOME/.local"
```

请确保 `~/.local/bin` 在 PATH 中。也可以直接运行 `./build/bgx`。

卸载时使用与安装相同的路径，例如 `make uninstall PREFIX="$HOME/.local"`。

## 使用

| 命令 | 用途 |
| --- | --- |
| `bgx start NAME -- PROGRAM [ARGS...]` | 启动后台命令，成功时静默返回 |
| `bgx list` | 列出名称、状态、PID、运行时长和命令 |
| `bgx log NAME [--tail N]` | 输出完整日志，或最后 N 行，然后退出 |
| `bgx stop NAME` | 停止任务，保留记录和日志 |
| `bgx remove NAME` | 删除已退出任务的记录和日志 |
| `bgx remove NAME --force` | 强制删除记录和日志，**不会停止进程** |
| `bgx help [COMMAND]` / `bgx version` | 查看帮助或版本 |

别名：`start` → `s`，`list` → `ls` / `ps`，`remove` → `rm`，`log` → `logs`，
`help` → `-h` / `--help`，`version` → `-V` / `--version`。
`bgx help COMMAND` 或 `bgx COMMAND --help` 查看命令用法，别名同样适用。

名称在同一用户的状态目录内共享，允许 1–128 个 ASCII 字母、数字、下划线或短横线，
以字母或数字开头，区分大小写：`Web` 和 `web` 是不同任务。
运行中的名称不能重复使用；任务退出后可复用名称，但会先删除旧记录和日志，即使新命令启动失败也不恢复。

启动时必须使用 `--` 分隔命令。任务继承当前目录和环境，不接收终端输入，标准输出和错误合并到日志。

```sh
bgx start build -- sh -c 'make && make test'
bgx log build | less
bgx log build --tail 100
```

## 边界

- 启动成功表示程序已执行，不代表服务已经就绪或任务最终成功。
- `stop` 先请求正常退出，等待 5 秒后仍未退出则强制终止，再等待最多 2 秒。
  同一进程组的子进程也会停止，主动脱离进程组的程序不在管理范围内。
- 状态为 `running`、`exited`、`stale`（进程身份不匹配）或 `unknown`（无法确认）。
  后两种情况拒绝停止；请先检查实际进程，必要时用 `remove --force` 清理记录。
- 不负责自动重启、开机恢复或交互式终端接管，也不记录任务的最终退出码。

## 数据位置

默认保存在 `~/.local/state/bgx`；设置了 `XDG_STATE_HOME` 时使用其下的 `bgx` 目录，
也可用 `BGX_STATE_HOME` 直接指定独立目录。

```text
<状态目录>/
  instances/<名称编码>/
    meta.json       # 任务记录
    output.log      # 合并日志
  locks/            # 并发操作锁
```

任务退出后数据仍会保留，直到删除或复用名称。开发期不保证数据格式兼容。

## 开发

C17 源码统一由 Zig 构建。测试需要 Python 3，额外的内存与未定义行为检查使用 Clang。

```sh
make build      # 优化并去除符号，输出 build/bgx，等同于 make
make debug      # 关闭优化、保留调试信息，输出 build/debug/bgx
make test       # 构建并运行集成测试
make sanitize   # 使用 Clang 运行 ASan/UBSan 检查
make check      # 运行以上两套检查
make clean      # 删除默认 build/、dist/ 目录
make TARGET_OS=linux TARGET_ARCH=arm64 BUILD=build/linux-arm64
```

Linux 始终静态链接 musl。`make MODE=debug test` 可测试调试版本；Clang 检查始终保留调试信息。
`clean` 不清理这两个目录之外的自定义 `BUILD` 路径，也不清理任务数据。
`make release TAG=vX.Y.Z` 用于发行打包，始终使用优化并去除符号的版本。
