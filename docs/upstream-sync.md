# 上游组件同步

`ArcaenBox Upstream Sync` 由维护者手动触发，检查内置协议组件的上游发布。发现新版本后，它会更新
`buildScript/builtins/versions.json` 中的版本、下载地址与 SHA-256，递增该渠道的
`revision`，重新编译四种架构并发布签名组件包。已经安装应用的用户可以在
「内核管理」中直接更新，无需等待新的 APK。

工作流文件：`.github/workflows/sync-upstreams.yml`
同步脚本：`.github/scripts/sync_upstreams.py`

成功发布时不保留组件产物——签名包已经在组件发布里。仓库是私有库，Actions 产物配额有限，
因此工作流产物只保留 1–3 天，历史占用需要在 Actions 页面或 API 中清理。

## 可自动同步的组件

| 组件 | 上游 | 版本来源 | 产物 |
| --- | --- | --- | --- |
| NaïveProxy | `klzgrad/naiveproxy` | 最新正式 Release | 官方插件 APK 中提取 `libnaive.so` |
| Snell（正式渠道） | `MetaCubeX/mihomo` | 最新正式 Release | 官方 Android `.gz` 可执行文件 |
| Trojan-Go | `SagerNet/SagerNet` 的 `trojan-go-plugin-*` 标签 | 最新插件标签 | 插件 APK 中提取 `libtrojan-go.so` |
| Mieru | `enfein/mieru` | 最新正式 Release | 锁定提交的源码，CI 编译 |
| Xray | `XTLS/Xray-core` | 最新正式 Release | 锁定提交的源码，CI 编译 |

脚本为每个组件独立探测。上游改了资产命名、缺少某种架构或标签无法解析时，该组件标记为
「检查失败」，整次运行失败，不会写入任何固定版本。

## 需要人工处理的组件

| 对象 | 原因 |
| --- | --- |
| Snell 测试渠道（sing-snell） | 实际编译字节来自 `buildScript/snell-client/go.mod` 的依赖版本，升级需要同步 Go 依赖并复核 Snell v6 适配器，不能只改 `versions.json` |
| sing-box 正式 / 测试内核 | 基于 MatsuriDayo Android 分支并叠加 `buildScript/lib/core/patches/`，升级上游后必须重新适配补丁、更新 `get_source_env.sh` 与 `CORE_REVISION`，再走内核与 APK 的完整验证链路 |
| libneko / gomobile | JNI 桥与 gomobile 补丁的构建工具链，改动需要重新验证 `Seq.class` 加载路径 |

这些对象只会在运行摘要的「内核提示」或「需人工处理的组件」中出现，不会被自动改写。
内核升级流程见 [`core-updates.md`](core-updates.md)。

## 一次同步做什么

1. **探测**：读取每个上游的最新版本、资产列表与发布时间，与锁定值逐字段比较（版本、
   下载地址、哈希、提交、Go 工具链）。
2. **观察期**：默认等待上游发布满 24 小时才允许改写。刚发布的版本会标记为「观察期」并
   跳过，避免把发布几分钟内的坏包推给用户。确认要立即同步时，把 `min_age_hours` 设为 `0`。
3. **改写**：只修改确实变化的组件，递增对应 `revision`，并同步
   `docs/built-in-protocols.md`（以及 Xray、Mieru 相关文档里的版本号）。
4. **构建**：`package_builtins.py` 下载并校验 SHA-256、ELF 架构与 PIE 格式；Mieru 与
   Xray 从锁定源码编译四种 ABI。
5. **签名**：`package_components.py` 生成每个组件的四架构签名包与清单。
6. **提交**：构建成功后，才把 `versions.json` 与文档提交到 `main`，然后重试推送。
7. **发布**：`publish_components.py` 按本次实际改写的 `component:channel` 创建
   `component-<组件>-<渠道>-r<修订号>` 不可变发布。

提交与发布使用同一份「本次实际改写」清单，而不是探测阶段的清单。这样即使上游在构建的
几十分钟里又发了新版本，也不会出现「提交了修订号却没有对应发布」的空档，新版本留到下一次
运行处理。

构建失败时不会提交，也不会发布；下一次运行会重新尝试同一版本。发布步骤只为本次变化的
组件创建新修订号，未变化的渠道继续使用已有发布，因此重复运行是幂等的。

Snell 的两个渠道共用 `snell` 这个组件名：`snell` 对应 `snell:stable`，`snell-preview`
对应 `snell:preview`。工作流按修订号后缀选择渠道，因此不会把测试渠道的字节发到正式渠道。

## 运行方式

工作流只保留 `workflow_dispatch`，不会按时间自动运行。在 Actions 中选择
**ArcaenBox Upstream Sync** → **Run workflow**，选好分支后填写输入：

| 输入 | 说明 |
| --- | --- |
| `publish` | 构建成功后是否提交固定版本并发布签名组件包。关闭时只构建并上传 `NativeComponentUpdates` 产物（保留 1 天）供检查，`main` 与渠道发布都不改动 |
| `components` | 可选，逗号分隔的组件 id：`naive,snell,trojan-go,mieru,xray,snell-preview`；留空检查全部 |
| `min_age_hours` | 上游发布后等待多少小时才同步，默认 `24` |

提交与发布是一对不可拆分的动作：只提交新的 `revision` 而不发布，会让随后构建的 APK
引用一个不存在的组件发布。因此关闭 `publish` 时工作流也不会提交。

只检查不构建，可在本地运行：

```sh
python3 .github/scripts/sync_upstreams.py --check --summary /tmp/report.md
python3 .github/scripts/sync_upstreams.py --self-test   # 离线自检
```

本地运行时建议设置 `GH_TOKEN`，否则匿名 API 限额只有每小时 60 次。

## 修订号与不可变发布

`revisions` 中的 `component:channel` 是 ArcaenBox 的适配修订号，不是上游版本。任何会导致
字节变化的修改（版本、下载地址、哈希、编译参数）都必须递增修订号；已发布的同名标签不会
被覆盖，`publish_components.py` 会发现标签已存在并拒绝替换不同字节。

同一版本重新编译（例如 Go 补丁变化）也必须递增修订号。工作流只在组件确实变化时递增，
未变化的组件继续使用已有发布，因此重复运行是幂等的。

## 失败与恢复

- **探测失败**：运行摘要列出失败组件，工作流失败且不写入任何内容。
- **构建失败**：工作区里的新固定版本不会提交，`main` 保持可用。
- **发布失败**：固定版本已提交，失败时会自动上传 `NativeComponentUpdates` 产物（保留 1 天）。可在
  **ArcaenBox Native Components** 中填入本次运行 ID 与 `source_run`，重新发布同一份签名包，
  无需重新构建。补齐发布之前不要用该提交构建新的 APK，否则内置清单会引用尚不存在的
  组件发布。
- **推送冲突**：工作流会 `pull --rebase` 后重试三次；仍失败时需人工解决冲突后重跑。

工作流使用仓库自带的 `GITHUB_TOKEN` 直接推送到 `main`，因此不会触发其他工作流。若 `main`
启用了分支保护，需要允许 GitHub Actions 推送，或改为在 PR 中审阅固定版本后手动发布。

## 新增一个自动同步组件

1. 在 `versions.json` 增加条目，并在 `revisions` 增加 `<id>:<channel>`。
2. 在 `sync_upstreams.py` 中增加 `Spec`、探测函数与 `PROBES` 映射；探测函数返回
   `version`、`source` 以及 `assets`（四架构 URL 与 SHA-256）或源码 `commit`/`url`/`sha256`。
3. 在 `package_builtins.py` 的 `identities` 中登记组件 id、渠道、最低 API 与协议版本。
4. 在 `doc_row`/`rewrite_doc` 中补充文档行，运行 `--self-test`，再手动跑一次 `--check`。
