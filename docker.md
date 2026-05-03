# Docker / OrbStack 日常操作手册（macOS）

> 环境：macOS + OrbStack（提供 Docker CLI 和 Linux 虚拟机运行时，比 Docker Desktop 轻量、快、省电）
>
> 本文档结合一次实战（NewAPI 容器卸载）沉淀出来的常用命令与踩坑记录。

---

## 0. 前言

OrbStack 对开发者 100% 兼容官方 Docker CLI，`docker ps` / `docker compose` / `docker volume` 等命令都直接可用。本手册分三部分：

1. **实战复盘**：NewAPI 卸载的完整过程（含我踩过的坑）
2. **日常运维命令速查**：容器 / 镜像 / 卷 / 网络 / compose
3. **排错手册**：遇到问题按症状查表

---

# 一、实战复盘：卸载 OrbStack 里的 NewAPI 容器

## 1.1 背景

- 昨天用 OrbStack 跑了个 `calciumion/new-api:latest`，映射到主机 3000 端口。
- 改用 LiteLLM + DeepSeek V4 Pro 后，NewAPI 不再需要，要清理掉。
- 目标：停容器 → 删容器 → 删镜像 → 删 compose 目录 → 释放 3000 端口。

## 1.2 侦查阶段（只读，先摸清现状）

```bash
# 1) 看谁在占 3000 端口
lsof -iTCP:3000 -sTCP:LISTEN

# 2) 运行中的容器 + 端口映射
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"

# 3) 所有容器（含已停止）
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"

# 4) 所有相关镜像
docker images | grep -iE "new-api|one-api|calciumion|songquanpeng"

# 5) 所有相关数据卷
docker volume ls | grep -iE "new|one"

# 6) 找 compose 文件
find ~ -maxdepth 4 -type f \( -name "docker-compose*.yml" -o -name "compose*.yml" \) 2>/dev/null | grep -iE "new|one"
```

**侦查结果**：
- 容器名：`new-api`
- 镜像：`calciumion/new-api:latest`（270MB）
- 端口映射：`0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp`
- 状态：`Up 23 hours`
- compose 文件：`/Users/maerun/Documents/.new-api/docker-compose.yml`
- 数据卷：无命名卷（全是 bind mount 到 `.new-api/` 目录）

## 1.3 第一次尝试：`docker compose down`（失败）

```bash
cd /Users/maerun/Documents/.new-api
docker compose down -v --rmi all
```

实际输出：

```
WARN[0000] /Users/maerun/Documents/.new-api/docker-compose.yml:
         the attribute `version` is obsolete, it will be ignored...
[+] down 1/1
 ! Image calciumion/new-api:latest Resource is still in use
```

**诊断**：
- `down 1/1` 这里的 `1/1` 只指网络，**没列任何容器**——说明 compose 没识别出归它管的容器
- 容器 `new-api` 依然 `Up 23 hours`，3000 端口还被占着
- 镜像删不掉的原因就是「还被活着的容器引用」

**根因**：Docker Compose 按 `project name` 识别容器。目录名 `.new-api`（带点）生成的 project name 跟当初启动容器时的 project label 不匹配，导致 `compose down` 找不到容器。

## 1.4 正确的解法（按容器名直接操作）

```bash
# 1) 停容器
docker stop new-api

# 2) 删容器（-v 同时清匿名卷）
docker rm -v new-api

# 3) 删镜像
docker rmi calciumion/new-api:latest

# 4) 删 compose 目录（含 bind mount 的数据）
cd ~
rm -rf /Users/maerun/Documents/.new-api
```

## 1.5 验证彻底干净

```bash
docker ps -a --format "table {{.Names}}\t{{.Image}}" | grep -iE "new-api|calciumion" || echo "✓ 无 NewAPI 容器"
docker images | grep -iE "new-api|calciumion" || echo "✓ 无 NewAPI 镜像"
lsof -iTCP:3000 -sTCP:LISTEN || echo "✓ 3000 端口空"
ls /Users/maerun/Documents/.new-api 2>&1 | grep -q "No such" && echo "✓ 目录已删"
```

四个 `✓` 全齐就彻底收工。

## 1.6 这次踩坑的 takeaway

| 教训 | 记住的做法 |
|---|---|
| `docker compose down` 不一定找得到容器 | 单容器场景直接用 `docker stop <name>` + `docker rm <name>` 最稳 |
| "Resource is still in use" 删不掉镜像 | 先确认引用它的容器是否真的停了/删了，别信 compose 的汇总 |
| compose 文件目录带 `.` 开头 | 会让 project name 解析变复杂，取名最好用纯字母 |
| bind mount 的数据要一并删 | `docker compose down -v` 只删命名卷，bind mount 到主机的目录要 `rm -rf` |

---

# 二、日常运维命令速查

## 2.1 查看状态

```bash
# 看 docker 是否正常连接
docker version
docker info | head -20

# 运行中的容器
docker ps

# 所有容器（含已停止）
docker ps -a

# 指定列（端口、状态最有用）
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"

# 看某个容器详细信息（JSON）
docker inspect <容器名> | less

# 看某个容器的 compose project label（判断它归哪个 compose 管）
docker inspect <容器名> --format '{{index .Config.Labels "com.docker.compose.project"}}'
```

## 2.2 容器启停

```bash
# 启动已存在的容器
docker start <容器名>

# 停止容器（优雅，默认 10 秒超时）
docker stop <容器名>

# 强制杀死（立即 SIGKILL，数据可能丢）
docker kill <容器名>

# 重启
docker restart <容器名>

# 进入容器交互 shell（调试用）
docker exec -it <容器名> /bin/bash    # 或 /bin/sh / /bin/ash

# 看容器实时日志
docker logs -f <容器名>

# 看最近 100 行日志
docker logs --tail 100 <容器名>

# 看容器里的进程
docker top <容器名>

# 看容器资源占用（CPU / 内存）
docker stats <容器名>
```

## 2.3 容器删除

```bash
# 删除已停止的容器
docker rm <容器名>

# 强制删除运行中的容器（等价于 kill + rm）
docker rm -f <容器名>

# 删除并清掉容器的匿名卷
docker rm -v <容器名>

# 一键清掉所有已停止的容器
docker container prune -f
```

## 2.4 镜像管理

```bash
# 列出所有镜像
docker images

# 看镜像的每一层
docker history <镜像名>

# 下载镜像
docker pull <镜像名>:<tag>

# 删除镜像
docker rmi <镜像名>:<tag>

# 强制删（不管有没有容器在用，用时小心）
docker rmi -f <镜像名>

# 按 ID 删（镜像被多个 tag 指向时用）
docker rmi <IMAGE_ID>

# 清理所有未被任何容器使用的镜像
docker image prune -a -f

# 只清 dangling（悬空）镜像，安全
docker image prune -f
```

## 2.5 卷（Volume）管理

```bash
# 列出所有命名卷
docker volume ls

# 看卷的详情（含主机挂载路径）
docker volume inspect <卷名>

# 删除命名卷
docker volume rm <卷名>

# 清理所有未使用的卷（安全，不会删被容器使用的）
docker volume prune -f

# 清理所有未使用的卷（含匿名卷，一起删）
docker volume prune -a -f
```

## 2.6 网络管理

```bash
# 列出所有网络
docker network ls

# 看网络详情（会列出里面的容器）
docker network inspect <网络名>

# 删除自定义网络
docker network rm <网络名>

# 清理所有未使用的网络
docker network prune -f
```

---

# 三、Docker Compose 操作

## 3.1 基础

```bash
cd <有 docker-compose.yml 的目录>

# 启动（后台）
docker compose up -d

# 启动 + 强制重建（配置改了之后用）
docker compose up -d --force-recreate

# 启动 + 强制拉取新镜像
docker compose up -d --pull always

# 停止（容器还在，能 start 回来）
docker compose stop

# 停止 + 删除容器 + 删除网络
docker compose down

# 停止 + 删除容器 + 删除网络 + 删除命名卷（💣 数据会丢）
docker compose down -v

# 停止 + 删除容器 + 删除网络 + 删除卷 + 删除镜像
docker compose down -v --rmi all

# 只 down 但保留孤立容器（避免误删）
docker compose down --remove-orphans
```

## 3.2 实用技巧

```bash
# 看 compose 管的所有容器
docker compose ps

# 看某个服务的日志
docker compose logs -f <服务名>

# 看全部服务日志
docker compose logs -f --tail 100

# 进入某个服务的容器
docker compose exec <服务名> /bin/bash

# 只重启某个服务
docker compose restart <服务名>

# 只重建某个服务
docker compose up -d --force-recreate <服务名>

# 看 compose 会解析成什么（含默认值、环境变量替换后）
docker compose config
```

## 3.3 project name 陷阱

**compose 按 project name 识别归它管的容器**，默认 project name = 当前目录名（去掉特殊字符）。

如果 `docker compose down` 对不上容器：

```bash
# 方法 1：看容器身上的 project label
docker inspect <容器名> --format '{{index .Config.Labels "com.docker.compose.project"}}'

# 方法 2：用 -p 显式指定 project name
docker compose -p <实际project名> down -v --rmi all

# 方法 3：显式指定 compose 文件路径
docker compose -f /path/to/docker-compose.yml down -v

# 方法 4：单容器场景直接用 docker stop/rm 按名字操作（最稳）
docker stop <容器名> && docker rm -v <容器名>
```

---

# 四、排错手册（按症状查）

## 4.1 `lsof -iTCP:<端口>` 显示 `OrbStack` 在监听

不是 OrbStack 自己的服务，是它代理了 Linux VM 里容器的端口。看是哪个容器:

```bash
docker ps --format "{{.Names}} {{.Ports}}" | grep <端口>
```

找到容器后按名字停/删即可。

## 4.2 "Cannot connect to the Docker daemon"

OrbStack 没启动。

```bash
# 启动
orb start

# 或打开 OrbStack.app
open -a OrbStack

# 看状态
orb status
```

## 4.3 "permission denied while trying to connect to docker.sock"

常见于 IDE 内置终端的沙箱环境（Cursor、VSCode 的 Restricted Mode）。换到系统原生终端（Terminal.app / iTerm）即可。

## 4.4 "Resource is still in use" 删不掉镜像

说明有容器（运行中或已停止）还在引用它。

```bash
# 查谁在引用这个镜像
docker ps -a --filter "ancestor=<镜像名>" --format "table {{.Names}}\t{{.Status}}"

# 把它们全杀了
docker ps -a --filter "ancestor=<镜像名>" -q | xargs -r docker rm -f

# 再删镜像
docker rmi <镜像名>
```

兜底强删:

```bash
docker rmi -f <镜像名>
```

## 4.5 "image has dependent child images"

删父镜像时有子镜像引用。

```bash
# 列出所有镜像，找子镜像
docker images --tree 2>/dev/null || docker image ls --format "{{.Repository}}:{{.Tag}} {{.ID}}"

# 清理所有未使用的镜像（父子一起清）
docker image prune -a -f
```

## 4.6 `docker compose down` 不生效

看第 3.3 节——99% 是 project name 不匹配。直接 `docker stop <name>` 更快。

## 4.7 容器反复自动重启

```bash
# 看重启策略
docker inspect <容器名> --format '{{.HostConfig.RestartPolicy.Name}}'

# 临时关掉重启策略
docker update --restart=no <容器名>
docker stop <容器名>
```

## 4.8 端口冲突 "bind: address already in use"

```bash
# 找谁在占端口
lsof -iTCP:<端口> -sTCP:LISTEN

# 如果是 Docker 容器，找出来
docker ps --format "{{.Names}} {{.Ports}}" | grep <端口>

# 停掉它或改端口映射
docker stop <冲突容器>
```

## 4.9 磁盘空间不够

```bash
# 看 Docker 总占用（镜像+容器+卷+构建缓存）
docker system df

# 看更详细的占用
docker system df -v

# 一键清理未使用的所有资源（不删卷，安全）
docker system prune -a -f

# 连未使用的卷也一起清（💣 注意！）
docker system prune -a -f --volumes
```

---

# 五、OrbStack 特有命令

```bash
# 启动 OrbStack（和它管理的 Linux VM）
orb start

# 停止（释放 CPU/内存，不损坏容器）
orb stop

# 看状态
orb status

# 重启
orb restart

# 退出 OrbStack.app（GUI）
osascript -e 'quit app "OrbStack"'

# 重新打开
open -a OrbStack

# 开机自启设置（GUI 里也能改）
# OrbStack 菜单栏 → Settings → General → Start at login
```

**OrbStack vs Docker Desktop**:

| 维度 | OrbStack | Docker Desktop |
|---|---|---|
| 启动时间 | 2-3 秒 | 15-30 秒 |
| 内存占用 | ~300MB 空闲 | ~1-2GB 空闲 |
| 磁盘占用 | 紧凑 | 臃肿 |
| 个人免费 | ✅ | ✅（企业需付费）|
| Docker CLI 兼容 | ✅ 100% | ✅ |
| Kubernetes 支持 | ✅ | ✅ |

---

# 六、清理 / 瘦身（定期做）

## 6.1 保守清理（只动"肯定不用"的）

```bash
# 悬空镜像（没 tag，通常是旧构建残留）
docker image prune -f

# 已停止的容器
docker container prune -f

# 未被任何容器使用的网络
docker network prune -f
```

## 6.2 激进清理（删"没容器在用"的一切）

```bash
# 清掉所有未被容器引用的镜像（含有 tag 的）
docker image prune -a -f

# 清掉所有未使用的卷（⚠️ 数据可能丢）
docker volume prune -f

# 一键全清（不动卷，相对安全）
docker system prune -a -f
```

## 6.3 核弹清理（只在你确定 Docker 里没重要东西时用）

```bash
# 停掉所有容器
docker stop $(docker ps -q) 2>/dev/null

# 删掉所有容器
docker rm -v $(docker ps -aq) 2>/dev/null

# 删掉所有镜像
docker rmi -f $(docker images -q) 2>/dev/null

# 删掉所有卷
docker volume rm $(docker volume ls -q) 2>/dev/null

# 或者一步到位
docker system prune -a -f --volumes
```

---

# 七、彻底卸载 OrbStack

如果你以后完全不用 Docker 了:

```bash
# 1) 先退出 OrbStack
osascript -e 'quit app "OrbStack"'
sleep 2

# 2) 移除 OrbStack.app
rm -rf /Applications/OrbStack.app

# 3) 清理用户数据（⚠️ 所有镜像、容器、卷一并删除）
rm -rf ~/.orbstack
rm -rf ~/Library/Group\ Containers/*.OrbStack
rm -rf ~/Library/Containers/dev.kdrag0n.*
rm -rf ~/Library/Caches/dev.kdrag0n.*
rm -rf ~/Library/Preferences/dev.kdrag0n.*

# 4) 从 PATH 里移除 orb CLI（如果还有残留）
rm -f /usr/local/bin/orb /usr/local/bin/orbctl
```

---

# 八、快速参考卡片

## 诊断三连

```bash
lsof -iTCP:<端口> -sTCP:LISTEN
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"
docker images
```

## 清理四连（按名字精准操作）

```bash
docker stop <name>
docker rm -v <name>
docker rmi <image>:<tag>
rm -rf <compose 目录>
```

## compose 标准流程

```bash
cd <compose目录>
docker compose up -d        # 启
docker compose logs -f      # 看日志
docker compose down -v      # 停（含卷）
```

## 省钱省电（不常用 Docker 时）

```bash
orb stop                    # 停 Linux VM，释放资源
# 要用再：orb start
```

---

# 九、实战案例存档

## 9.1 NewAPI 卸载完整流程（本文档编写缘由）

```bash
# 侦查
lsof -iTCP:3000 -sTCP:LISTEN
docker ps -a | grep new-api

# 清理（compose down 不生效，改走按名操作）
docker stop new-api
docker rm -v new-api
docker rmi calciumion/new-api:latest
rm -rf /Users/maerun/Documents/.new-api

# 验证
docker ps -a | grep new-api || echo "✓"
docker images | grep new-api || echo "✓"
lsof -iTCP:3000 -sTCP:LISTEN || echo "✓"
ls /Users/maerun/Documents/.new-api 2>&1 | grep -q "No such" && echo "✓"
```

---

> 最后更新：2026-05-02
> 维护人：maerun
>
> 本文档结合 OrbStack + Docker CLI 实战整理，持续更新。
