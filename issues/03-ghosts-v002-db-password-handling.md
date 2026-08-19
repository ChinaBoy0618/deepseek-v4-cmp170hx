# Issue 3 — ghosts_v002 / pgvector-ai postgres 连接密码不一致（760 服务器）

> **本 issue 全部凭据已隐藏**：真实的连接密码不会出现在文档或代码里。所有引用一律用
> `${DB_PASSWORD}` 占位。**实际值请到 760 服务器后端运行时 env / docker inspect / 进程
> 环境变量中查询**，不在仓库任何地方硬编码。

## 现象

760 服务器（`760T` = `47.99.74.105:6000`）上的 postgres 容器 **`pgvector-ai`** 与
assistant 后端共用同一个库 `ghosts_v002`。本机开发/调试时遇到的密码问题：

1. assistant 后端代码 / `.env.example` 里出现的开发默认口令占位（开发文档里曾以
   示例性弱口令占位；为消除混淆，仓库内一律改成 `${DB_PASSWORD}`）与 `pgvector-ai`
   容器**实际设置的 postgres 密码不一致**。
2. 直接用开发默认口令连 `pgvector-ai:5432/ghosts_v002` 会被拒。
3. 真实口令**只能从 assistant 后端的运行时 env 拿到**（进程环境变量 / docker inspect 注入
   的 env / docker-compose env_file 间接源）。
4. 该库被多服务共享（assistant 后端、pgvector-ai、以及其它依赖向量检索的组件），**禁止
   改 postgres 密码**（一旦改了，所有共享服务都得改并重启，且可能丢连接池 / Vector 索引
   一致性）。

## 影响

- 本地联调连 `ghosts_v002` 走默认口令连不上，必须改成"从后端运行时 env 取"。
- 文档 / 调试指南 / README 里不应再出现"开发默认密码 = `<明文>`"这种诱导，**全部替换为
  `${DB_PASSWORD}` + 指引"到 760 服务器查运行时 env"**。
- CI / 自动化如果硬编码默认口令，会持续失败；应改为从 CI secret 注入或从远端 env 同步。

## 建议的修复

1. **仓库里移除任何真实密码字面量**：
     - 后端 `.env.example` / 模板里只留 `${DB_PASSWORD}` 占位 + 注释
       ```bash
       # DB_PASSWORD: 实际值在 760 服务器 assistant 后端运行时 env 里；
       # 本仓库不保存任何真实口令。本机调试时从以下来源之一获取：
       #   ssh 760T 'docker inspect <assistant-backend> --format "{{.Config.Env}}"' | grep DB_PASSWORD
       #   或 assistant 后端进程 env
       ```
     - 文档 / README / 调试手册同样改占位，避免任何人把真实口令 commit 进来。
2. **运行时连接逻辑**：assistant 后端启动时从 env 读 `DB_PASSWORD`，**不要从仓库任何
   文件 fallback 到任何写死的示例/默认值**。
4. **CI**：CI secret 注入 `DB_PASSWORD`，本地/CI 行为对齐。
5. **共享库改密流程**（如果将来真要轮换）：
     - 先在维护窗口停所有共享服务 → 改 postgres 密码 → 更新所有注入 env 的源头 → 全部
       重启 → 验证向量索引 / 连接池。
     - 当前不在窗口内，**保持现状**，只在文档侧强化"不在仓库出现真实口令"。

## 排查命令（不在仓库里粘真实口令）

```bash
# 760 服务器上查 assistant 后端运行时实际注入的 DB_PASSWORD
ssh 760T 'docker inspect <assistant-backend-container> \
  --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -i DB_PASSWORD'

# 或对运行中的后端进程查 env
ssh 760T 'ps -eo pid,cmd | grep <assistant-backend> | grep -v grep'

# 验证连通性（口令从 env 注入，不要在命令行明文写）
ssh 760T 'PGPASSWORD="${DB_PASSWORD}" psql -h pgvector-ai -U <user> -d ghosts_v002 -c "\dt"'
```

## 参考

- 服务部署背景：本仓库 README / RESULTS（`dsv4-a100` 服务运行细节）。
- 760 服务器构建环境 / vLLM 部署笔记：~/.claude 项目的 `760-server-build-environment`
  memory 文件（不进 git，本机 ~/.claude 内存）。