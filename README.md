# ProjectHub

根据 `Z:\Code\DeviceBridge` 当前功能独立重写的文件与设备服务。全部业务代码重新编写，不导入或复制原工程的业务模块。采用 FastAPI、标准库 sqlite3 和明确的服务实例；保留 `/api/v1` 的 51 个业务接口；用户权限按下述型号授权规则执行。

主要功能包括用户与 14 项权限、型号/版本管理、四类文件、曲线与固件审核、固件升级、曲线排序、限时下载授权、frp/RFB 远控，以及 Windows 服务启停、迁移与文件同步。接口详情可在启动后的 `/docs`、`/redoc` 和 `/openapi.json` 查看。

## 功能模块

生产代码位于 `projecthub/`，顶层只保留包入口；每个功能目录通过 `__init__.py` 提供公开接口。

```text
projecthub/
├── application/   配置、服务装配、启动和关闭
├── database/      表结构、连接、事务、完整性检查
├── users/         用户资料、认证和密码
├── permissions/   权限目录、授权配置、实时权限检查
├── projects/      型号/版本查询、创建、改名及恢复
├── files/         文件查询、上传下载、审核发布、授权、同步
├── remote/        设备注册、租约、frp/RFB通信
├── http/          HTTP路由、参数、响应和并发门禁
├── runtime/       Windows进程启动、状态和停止
├── storage/       受管路径、文件读写、文件锁
└── contracts/     统一错误、JSON与任务完成契约
```

`Users` 负责账号，`Permissions` 负责权限，`ProjectService` 负责项目管理。`FileService` 持有文件资源，通过 `catalog`、`reviews`、`authorizations`、`synchronizer` 提供相应功能。跨模块使用公开接口，不读取其他模块私有状态。详细职责与调用关系见 [模块说明](docs/模块说明.md)。

## 型号权限与版本资源

权限统一配置到型号。同一型号的高配、低配、旗舰、定制等版本共用权限，资源分别存放在 `data/files/型号/版本/分类/文件名`；待审文件也按型号/版本隔离，不同版本允许同名文件。

普通用户在某型号拥有至少一项权限即可看到该型号及其全部版本，读取、上传和审核仍分别检查对应权限。没有授权的型号不出现在列表中，直接按ID访问其版本返回404。管理员能查看和管理全部型号，包括后来新增的型号和版本。固件和公共文件不再默认向所有用户开放读取。

管理员通过 `PUT /api/v1/users/{user_id}/permissions` 整体替换型号授权，签名请求的 `data` 例如：

```json
{
  "global_permissions": [],
  "project_permissions": {
    "curve.read": ["M910"],
    "curve.manage": ["M910"],
    "firmware.read": ["M910", "M920"]
  }
}
```

`global_permissions` 是固定为空数组的保留字段，非空请求会被拒绝；授权仅来自 `project_permissions`。版本配置 `/config` 也要求认证及型号授权。下载授权码仍可独立授予指定文件访问，不提供型号目录浏览权限。

当前数据库结构不变，原 `users.permissions` 列不再参与鉴权；不会自动把已有全局权限转换为所有型号授权。程序变更不迁移或修改已有运行数据。

## 环境与启动

支持 CPython `>=3.14.7,<3.15`；本次实际验证为 Windows、CPython 3.14.10。受管进程控制只支持 Windows。默认 HTTP 地址为 `0.0.0.0:8001`，业务并发 16，远控默认关闭。

在本目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe main.py --project-root Z:\ProjectHub
```

访问 `http://127.0.0.1:8001/docs`。服务首次启动创建本项目的 `data`，使用项目内定义的初始账号规则。HTTP 认证使用一次 UTF-8 百分号编码的 `X-User-Name` / `X-Password`；Swagger 的认证辅助会完成编码。管理请求的 `id/data/sha256` 及五字段响应与原版一致。

构造或导入应用不创建数据库；真正初始化由 ASGI lifespan 执行。安装 wheel 后须显式指定运行根目录，例如 `python -m main --project-root D:\Services\ProjectHub`。

## 配置

权威默认值位于 `projecthub/application/config.py` 和 `projecthub/remote/config.py`。配置为冻结 dataclass；显式构造 `AppConfig` 覆盖默认值。命令行再覆盖 `--host`、`--port`、`--project-root`。运行中不热更新，也不自动读取环境变量或隐藏配置文件。

自定义配置入口示例：

```python
import os
from pathlib import Path
from projecthub.application import run
from projecthub.application import AppConfig, HttpConfig, RemoteControlConfig

settings = AppConfig(
    project_root=Path("D:/Services/ProjectHub"),
    http=HttpConfig(port=8001, max_concurrent_requests=16),
    remote_control=RemoteControlConfig(
        enabled=True,
        public_host="devices.example.com",
        api_token=os.environ["PROJECTHUB_API_TOKEN"],
        frp_token=os.environ["PROJECTHUB_FRP_TOKEN"],
    ),
)
if __name__ == "__main__":
    run(settings)
```

启用远控前另安装 `requirements-remote.lock`。远控默认端口为 8002 / 8003；以上环境变量只由示例入口读取。令牌不应写入源码或日志。`main:app` 和 `service:app` 为源码运行的默认 ASGI 入口；生产使用单 worker。

## 运维

```powershell
python -m scripts.service_control start --project-root Z:\ProjectHub
python -m scripts.service_control status --project-root Z:\ProjectHub
python -m scripts.service_control stop --project-root Z:\ProjectHub
# 或使用部署脚本，-Python 可指定虚拟环境解释器
.\deployment\windows\start.ps1 -Python .\.venv\Scripts\python.exe
.\deployment\windows\stop.ps1 -Python .\.venv\Scripts\python.exe

# 离线维护，正在运行时会由项目锁拒绝
python -m scripts.migrate --project-root Z:\ProjectHub
python -m scripts.sync_files --project-root Z:\ProjectHub
python -m scripts.sync_files --project-root Z:\ProjectHub --metadata metadata.json
```

同步的 metadata 是以 `型号/版本/分类/文件名` 为键、允许的业务元数据为值的 JSON 对象。首次扫描登记型号、版本及 internal/public 文件；curve/firmware 必须经审核，离线同步不能绕过审核发布。

数据布局保留 `data/sql/device_bridge.sqlite3`、`data/files/型号/版本/分类/文件名`、`data/audit`、`data/backups`、`data/logs`。受管进程的输出保存在 `data/logs/service.stdout.log`、`service.stderr.log`。启动恢复本项目自身未完成的事务清单；遇到未知内容或恢复失败会拒绝继续文件操作，保留现场供排查，恢复条件消除后重启即可重试。

本工程按全新部署交付，不提供旧版数据库迁移、旧待审记录和旧恢复清单兼容。请使用独立的运行根目录，不接入 DeviceBridge 的历史 data。`scripts.migrate` 用于新库初始化及当前结构检查，不执行旧版本升级。本次未搬迁或读取原工程的真实业务数据。

## 检查与构建

```powershell
python -m pip install -r requirements-dev.lock
python -m scripts.check
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

`check` 运行 Ruff 静态/格式检查、字节编译和 pytest。功能对照测试在独立进程和临时目录运行当前参考服务和本项目；默认参考路径为 `Z:\Code\DeviceBridge`，也可用 `PROJECTHUB_REFERENCE_ROOT` 指定。参考工程不存在时仅该测试跳过；运行对照还需原工程自身依赖。所有独立业务测试均不依赖原工程。

更多内容：[项目开发约定](项目开发约定.md)、[功能与验证报告](docs/功能与验证报告.md)、[开发规范](通用软件开发规范.md)。
