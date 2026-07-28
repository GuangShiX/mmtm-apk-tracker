# 独白与回忆动作配置

`action_config.py` 从已经满足 `complete` 不变量的提取结果中生成可读动作配置。

这里没有额外的密码学加密层：

- 回忆页面动作是 `ScenarioScript` MonoBehaviour TypeTree；
- 角色进入、循环、退出和独白动作是 Unity `AnimationClip` 的
  `StreamedClip` / `ConstantClip` 曲线；
- 角色专属的混合、基准点、特效和视线参数分别位于
  `*_blend.asset`、`*_basepoint.asset`、`*_effect.asset` 和
  `*_look.asset`。

## 使用

```powershell
python action_config.py 4.18.0
```

独白的歌词与朗读切换时间属于 Master 数据，不在 APK 的 Unity 对象内。已有
`MonologueMB.json` 时可以一并传入：

```powershell
python action_config.py 4.18.0 `
  --monologue-master D:\data\Master\MonologueMB.json
```

默认输出到 `reports/<version>_action_configs/`：

```text
index.json                  # 覆盖率、来源和全部文件索引
enum_maps.json              # 数字枚举到动作/情绪名称的映射
memory_scenarios/           # 带枚举名称的完整回忆页面脚本
characters/                 # 每名角色的 blend/basepoint/effect/look
motions/<CHR_*>/            # Memory_* / Monologue_Loop.motion3.json
monologue_master.json       # 仅在传入 MonologueMB.json 时生成
```

`motion3.json` 曲线可直接用于 Cubism 工作流。转换保留原始时长、循环标记、
事件和曲线顺序。绑定名通过 Unity 路径 CRC32 与模型对象名反查；无法唯一反查
的少量曲线不会被丢弃，而是以 `PathHash_XXXXXXXX` 命名，并记录在
`index.json` 的 `unresolved_bindings` 中。

输出目录已存在时命令会拒绝覆盖。确认重建该精确目录时使用 `--force`。
