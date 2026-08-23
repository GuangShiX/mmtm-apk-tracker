# Helper 游戏美术统一资产链路优化提示词

将下面内容作为 `D:\NewProjects\memento\mementomori-helper-master` 新 session 的首条提示词：

```text
请在 D:\NewProjects\memento\mementomori-helper-master 中完成“所有游戏美术资源统一走本地优先资产链路”的审计和优化。

开始前必须完整阅读：
- AGENTS.md
- docs/ai/rules-inventory.md 的 Game Icon System 规则
- docs/游戏图标系统.md
- MementoMori.WebUI/Services/GitHubImageAssetService.cs
- CharacterIconService、EquipmentIconService、SphereIconService、GameIconRenderService
- MementoMori.Tests/GitHubImageAssetServiceAssertions.cs

当前工作树可能已有大量其它任务改动。先检查 git status 和相关文件 diff，不得覆盖、回退、暂存或顺手整理其它任务的修改。此次工作不需要启动 5700/5701，不发送游戏请求，不触碰账号或运行时数据；使用隔离输出完成源码测试和构建即可。

职责边界：
- mmtm-apk-tracker 负责官方 appVersion/assetVersion 发现、APK/Addressables 提取模式、允许清单与发布校验。
- GuangShiX/mmtm-assets-fallback 由 CI 发布规范 PNG、manifest.json、latest.json 和版本标签。
- helper 只是只读消费者：按需下载、严格校验、持久缓存和本地渲染。不得在 helper 配置 GitHub Token，不得自动提交资产仓库，也不得实现 APK 下载、Unity 解包或游戏版本检测。

不可改变的资源优先级：
1. 已存在的最终渲染缓存。
2. helper 随包提供或以前下载并验证过的本地源图片。
3. 统一 GitHubImageAssetService 查询 mmtm-assets-fallback。
4. 仅对明确支持的类型使用有界降级来源；Tamamo 只能位于 GitHub 规范源之后。
5. 不持久化为完整规范缓存的占位图/fallback SVG。

本地最终缓存或源图片有效时必须零网络：不得在启动时、后台定时、普通渲染、缓存命中时发送 manifest GET、图片 GET、HEAD 或版本探测。只有真实本地缺失或用户在 /Diagnostics 显式“重新检查”时才允许访问远端。

任务一：做真实调用点审计
- 搜索 Razor、CSS、ViewModel、服务和控制器中的 GitHub Raw/CDN/Tamamo URL、直接 wwwroot 游戏图片路径、CSS url(...)、独立 HttpClient 下载、重复 manifest/cache 实现。
- 将结果按 characters、enemies、equipment、items、spheres、ui、background/prefab-sprite 分类。
- 区分“随 helper 发布且稳定的本地产品素材”与“来自游戏包、应由资产仓库供应的游戏美术”；不要把字体、第三方网页、战报查看器等非游戏美术误纳入迁移。
- 在最终报告中列出每个调用点的当前来源、目标共享服务、是否需要迁移和理由。

任务二：补齐统一资产链路
- 所有新增或修改的游戏美术调用点必须复用 GitHubImageAssetService；页面不得直接拼 Raw/CDN URL，也不得新建页面级下载器、manifest 或缓存。
- 保留 CharacterIconService、EquipmentIconService、SphereIconService 等类型服务作为业务入口；它们共享一个 GitHubImageAssetService，不互相复制下载和校验代码。
- 重点核查 SphereIconService：本地 SPH_*.png 缺失时通过共享服务查询 spheres/SPH_*.png，下载后继续使用原 GameIconRenderService 合成；不得增加独立宝珠 downloader。
- 为页面 UI、背景、九切片和 Prefab Sprite 设计最小共享入口。请求必须有稳定 category/name/path 映射和来源页面；不要让 Razor/CSS 直接依赖远端 URL。已经随包提供且稳定的本地资产可继续作为优先本地源，不做无收益迁移。
- 将缺失记录中的角色专用身份字段扩展为兼容旧 JSON 的通用资源身份（例如 ResourceType/ResourceId），保留读取现有 CharacterId 记录的兼容性。
- 最终/合成缓存元数据必须记录全部规范源依赖 SHA-256。规范素材修复或显式重新检查成功后，精确失效依赖它的 Tamamo fallback、占位结果和最终合成缓存。

任务三：保持下载与缺图语义
- manifest 和图片只允许来自固定 GuangShiX/mmtm-assets-fallback Raw 基址；阻止路径穿越和跨主机 URL。
- 校验 HTTP 2xx、manifest path/category/name、5 MiB 单文件上限、PNG 签名、chunk CRC、可解压图像、size 和 SHA-256。
- 下载到目标目录临时文件，通过后原子替换；失败不能破坏旧的有效文件。
- 同一 manifest 刷新、同一源图片下载和同一最终合成请求分别合并并发。
- manifest 无条目、图片 404/410 或发布内容校验失败时创建一条持久缺失记录。
- 超时、断网、限流和 5xx 只能短暂冷却，不得成为永久缺失。
- Tamamo 或占位图成功不能把规范缺口标记为 Resolved，也不能伪装成 GitHub/local-source。
- 普通重复渲染不得反复请求已确认缺失的 GitHub/Tamamo；只能由显式重新检查恢复。
- /Diagnostics 保留重新检查、忽略、已提交、已解决和预填 GitHub Issue；不得无人工确认自动创建 Issue。

任务四：建立“发现缺图 -> 资产仓库补全”交接结果
- 对审计或测试发现的每个规范缺图，输出 category、name、expected path、来源页面/资源 ID、当前 fallback 和缺失原因。
- 判断归属：
  - manifest/规范 PNG 缺失：需要在 mmtm-apk-tracker 扩充提取模式或允许清单，再由资产 CI 发布。
  - 提取、哈希、路径、CI 发布错误：mmtm-apk-tracker 问题。
  - helper 名称映射、缓存、失效或渲染错误：helper 问题。
- 本 session 以 helper 优化为主。未经明确授权不要跨仓库提交或发布；如果确实需要 tracker 改动，给出可直接执行的文件、规则、测试和预期资产清单。

必须补充或更新回归断言，至少覆盖：
- 最终缓存命中零 HTTP。
- 本地源图片命中零 HTTP。
- 首次缺失下载一次、第二次只读本地。
- manifest/资源并发请求合并。
- 宝珠本地缺失后从 GitHub 下载并进入原合成链路。
- UI/Prefab Sprite 使用共享服务且不在页面拼远端 URL。
- 损坏 PNG、chunk CRC 错误、size/SHA 不符、超限、路径穿越和跨主机 URL被拒绝。
- 网络/5xx 不写永久缺失记录。
- 404 只产生一条缺失记录；Tamamo/占位不掩盖缺口。
- 显式重新检查取得规范资源后，缺失记录与依赖最终缓存准确更新。
- 旧 CharacterId 缺失记录 JSON 仍可读取并迁移为通用资源身份。

验证：
1. 先对 MementoMori.Tests 做隔离构建。
2. 运行生成的自定义断言程序：
   dotnet exec <built MementoMori.Tests.dll> --github-image-asset-only
3. 再使用新的隔离 artifacts 路径构建完整 MementoMori.sln：
   dotnet build .\MementoMori.sln --artifacts-path <isolated-path> -nodeReuse:false -p:UseSharedCompilation=false
4. 默认不要运行 --github-image-live；它会访问真实 GitHub，仅在用户明确授权实时网络验证后执行。
5. 运行 git diff --check，并报告所有修改文件、测试结果、未验证边界和仍需由 tracker 补齐的资产清单。

不要把本任务扩大为视觉重设计、账号逻辑、游戏 API、发布打包或无关告警清理。不要提交或推送，除非用户另行明确要求。
```
