# 自爆卡车

AstrBot 群聊趣味禁言插件：对轰互禁，空炸自罚并可能波及邻近发言者。

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-blue.svg)](https://docs.astrbot.app/)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-aiocqhttp%20%7C%20qq__official-orange.svg)](https://docs.astrbot.app/)
[![Version](https://img.shields.io/badge/version-1.7.1-green.svg)](https://github.com/DEVILENMO/astrbot_plugin_self_destruct_truck)

## 功能概览

- **正确对轰**：`/自爆卡车 @对方` → 发起者随机禁言，对方 = 总和 − 发起者
- **空炸 / 原地自爆**：没 @、@ 自己、或格式不对时，扫描邻近人类发言并按概率波及；发起者禁言 = 波及秒数总和（无人波及时随机自罚）
- **累加禁言**：优先接入 [弧光护卫](https://github.com/DEVILENMO/astrbot_plugin_arc_guard) 的累加接口（`respect_whitelist=false`，不豁免白名单）；未安装时由本插件直接禁言，`aiocqhttp` / `qq_official` 均可用
- **评级**：按对轰分配或波及伤害给出 S～F 评级文案

## 环境要求

- AstrBot `>= 4.0.0`
- Python `3.10+`
- 真禁言：`aiocqhttp` 需机器人有群禁言权限；`qq_official` 需把机器人设为群管理员（只能禁言普通成员）
- 推荐同时启用 `astrbot_plugin_arc_guard`（弧光护卫），以便走累加禁言；未启用时由本插件直接禁言，两个平台均可用

## 安装

### 链接安装

```text
https://github.com/DEVILENMO/astrbot_plugin_self_destruct_truck
```

### 手动安装

将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_self_destruct_truck/`，重载插件即可。

## 指令

指令前缀以你的 AstrBot 配置为准（常见为 `/`）。

| 指令 | 说明 |
|------|------|
| `/自爆卡车 @对方` | 对轰：双方按随机分配禁言 |
| `/自爆卡车`（无有效 @） | 原地自爆：自罚 + 可能波及邻近发言 |

别名：`selfdestruct`、`self_destruct_truck`。

## 规则说明

### 正确对轰

1. 发起者禁言秒数在配置的 `[min_damage, total_pool - 1]` 内随机
2. 被 @ 者禁言秒数 = `total_pool - 发起者秒数`（默认总和 60）
3. 评级看对方分到的占比（越高越接近「致命对轰」）

### 原地自爆（空炸）

1. 扫描邻近 `splash_range` 条**人类**消息（跳过机器人自身发言，不占名额；排除当前发起者）
2. 每条发言单独按 `splash_chance`% 判定；命中则禁言 `splash_duration` 秒
3. 同一人多次命中则秒数叠加
4. 发起者禁言 = 所有波及成功秒数之和；若无人被波及，则在 `[misfire_min, misfire_max]` 内随机自罚
5. 评级看波及总秒数相对理论上限（`splash_range × splash_duration`）

## 配置

| 配置项 | 默认值 | 说明 |
|------|------|------|
| `min_damage` | `10` | 对轰时发起者禁言下限（秒） |
| `total_pool` | `60` | 对轰双方禁言秒数总和 |
| `misfire_min` | `15` | 空炸且无人波及时，发起者自罚下限 |
| `misfire_max` | `45` | 空炸且无人波及时，发起者自罚上限 |
| `splash_range` | `3` | 空炸扫描邻近人类消息条数 |
| `splash_chance` | `10` | 每条发言被波及的概率（%） |
| `splash_duration` | `10` | 单次波及禁言秒数 |

## 更新记录

### 1.7.1

- 弧光护卫在场时，它的裁决是最终结果：拒绝（如超级管理员硬保护、联动机器人）后不再回退直连禁言；只有未安装弧光护卫时才由本插件直接禁言

### 1.7.0

- 未安装弧光护卫（或其禁言未成功）时，由本插件直接禁言，`aiocqhttp` 与 `qq_official` 均可用；官方平台走群管理接口 `restrict_chat_setting`，不会缩短已有的禁言
- 修复 `qq_official` 上 `@` 显示为原始标签：消息不再强制纯文本，跟随平台的 markdown 设置

## 常见问题

**提示失败 / 没禁言？**  
确认机器人能禁言群成员：`aiocqhttp` 需要群禁言权限，`qq_official` 需要把机器人设为群管理员。目标是群主/管理员时，官方平台无法禁言。

**白名单用户也能被炸？**  
可以。走弧光护卫时会显式关闭白名单豁免（`respect_whitelist=false`）。

**波及总是空？**  
群里近期人类发言太少或概率未命中时会出现，可调高 `splash_chance` / `splash_range`。波及对象来自插件自己记录的最近 50 条群发言；`aiocqhttp` 上记录为空时还会拉取群历史消息，`qq_official` 没有这个接口，所以插件刚重载后需要群里先有人说话。

## 目录结构

```text
astrbot_plugin_self_destruct_truck/
├── main.py              # 插件逻辑
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置 Schema
├── README.md            # 本文档
└── LICENSE
```

## 仓库

https://github.com/DEVILENMO/astrbot_plugin_self_destruct_truck

## 作者

DEVILENMO

## License

[MIT](./LICENSE)
