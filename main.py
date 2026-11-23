from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_VERSION = "1.7.1"
ARC_GUARD_PLUGIN = "astrbot_plugin_arc_guard"


@register(
    "astrbot_plugin_self_destruct_truck",
    "DEVILENMO",
    "自爆卡车：双方一起被禁言",
    PLUGIN_VERSION,
)
class SelfDestructTruckPlugin(Star):
    """自爆卡车插件。

    用法：
        /自爆卡车 @xxx

    - 正确 @ 别人：发起者随机禁言，对方 = 总和 - 发起者
    - 格式不对：发起者自己炸（禁言 = 波及者禁言秒数总和；无人波及时回退随机上下限），并波及邻近发言者
    - 禁言优先走弧光护卫累加接口（``respect_whitelist=False``，不豁免白名单）；
      弧光护卫不可用时由本插件直接禁言（aiocqhttp / qq_official 均支持）
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        # platform_id:group_id -> recent human speaker ids (oldest first)
        self._recent_speakers: dict[str, list[str]] = {}

    def _get_arc_guard_api(self):
        """Resolve Arc Guard accumulate-mute API when available.

        Returns:
            Arc Guard ``get_api()`` result, or None if unavailable.
        """
        try:
            meta = self.context.get_registered_star(ARC_GUARD_PLUGIN)
        except Exception as exc:  # noqa: BLE001
            logger.debug("自爆卡车：查找弧光护卫失败: %s", exc)
            return None
        if not meta or not getattr(meta, "activated", False):
            return None
        star = getattr(meta, "star_cls", None)
        if star is None:
            return None
        get_api = getattr(star, "get_api", None)
        if not callable(get_api):
            return None
        try:
            return get_api()
        except Exception as exc:  # noqa: BLE001
            logger.warning("自爆卡车：调用弧光护卫 get_api 失败: %s", exc)
            return None

    def _truck_params(self) -> tuple[int, int, int]:
        """Load normal truck mute range.

        Returns:
            (min_damage, max_damage, total_pool)
        """
        total_pool = max(2, int(self.config.get("total_pool", 60)))
        min_damage = max(1, int(self.config.get("min_damage", 10)))
        max_damage = total_pool - 1
        if min_damage > max_damage:
            min_damage = max_damage
        return min_damage, max_damage, total_pool

    def _misfire_self_params(self) -> tuple[int, int]:
        """Load fallback self-mute range when misfire hits nobody.

        Returns:
            (min_seconds, max_seconds) inclusive.
        """
        lo = max(1, int(self.config.get("misfire_min", 15)))
        hi = max(1, int(self.config.get("misfire_max", 45)))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def _splash_params(self) -> tuple[int, float, int]:
        """Load misfire splash params.

        Returns:
            (range_n, chance_percent, duration_seconds)
        """
        range_n = max(0, int(self.config.get("splash_range", 3)))
        chance = max(0.0, min(100.0, float(self.config.get("splash_chance", 10))))
        duration = max(1, int(self.config.get("splash_duration", 10)))
        return range_n, chance, duration

    def _collect_at_ids(self, event: AstrMessageEvent) -> list[str]:
        """Collect @ targets from At components, official <@openid> tags, and raw mentions."""
        at_ids: list[str] = []
        self_id = str(event.get_self_id() or "")

        def add_id(raw: object) -> None:
            qq = str(raw or "").strip()
            if (
                not qq
                or qq in {"all", "everyone", "qq_official", self_id}
                or qq in at_ids
            ):
                return
            at_ids.append(qq)

        try:
            for comp in event.get_messages():
                if isinstance(comp, At) and comp.qq:
                    add_id(comp.qq)
                elif isinstance(comp, Plain) and comp.text:
                    for match in re.finditer(r"<@!?([A-Za-z0-9_-]+)>", str(comp.text)):
                        add_id(match.group(1))
                    for match in re.finditer(
                        r"@(\d{5,}|[A-Fa-f0-9]{32})", str(comp.text)
                    ):
                        add_id(match.group(1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析 At 组件失败: %s", exc)

        if not at_ids:
            for match in re.finditer(r"<@!?([A-Za-z0-9_-]+)>", event.message_str or ""):
                add_id(match.group(1))
            for match in re.finditer(
                r"@(\d{5,}|[A-Fa-f0-9]{32})", event.message_str or ""
            ):
                add_id(match.group(1))

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mentions = None
        if isinstance(raw, dict):
            mentions = raw.get("mentions")
        elif raw is not None:
            mentions = getattr(raw, "mentions", None)
        if isinstance(mentions, list | tuple):
            for mention in mentions:
                if isinstance(mention, dict):
                    if mention.get("is_you") is True:
                        continue
                    add_id(
                        mention.get("id")
                        or mention.get("member_openid")
                        or mention.get("user_openid")
                    )
                    continue
                if getattr(mention, "is_you", False) is True:
                    continue
                add_id(
                    getattr(mention, "id", None)
                    or getattr(mention, "member_openid", None)
                    or getattr(mention, "user_openid", None)
                )
        return at_ids

    async def _official_mute(
        self, bot, group_openid: str, member_openid: str, seconds: int
    ) -> bool:
        """Mute a member on qq_official without Arc Guard.

        Uses the official group-admin API: the bot must be a group admin and
        only regular members can be muted. The current mute state is read
        first so an existing longer mute is never shortened.

        Args:
            bot: qq_official botpy client.
            group_openid: Group openid.
            member_openid: Member openid.
            seconds: Mute seconds.

        Returns:
            True if the mute API call succeeded.
        """
        if not (hasattr(bot, "api") and hasattr(bot.api, "_http")):
            return False
        try:
            from botpy.http import Route

            http = bot.api._http
            expire = int(time.time()) + max(1, int(seconds))
            op = "add"
            state = await asyncio.wait_for(
                http.request(
                    Route(
                        "GET",
                        "/v2/groups/{group_openid}/restrict_chat_setting",
                        group_openid=group_openid,
                    )
                ),
                timeout=5,
            )
            members = state.get("members") if isinstance(state, dict) else None
            for item in members if isinstance(members, list) else []:
                if (
                    not isinstance(item, dict)
                    or item.get("member_openid") != member_openid
                ):
                    continue
                op = "update"
                current = datetime.fromisoformat(
                    str(item.get("mute_expire_at") or "").replace("Z", "+00:00")
                )
                expire = max(expire, int(current.timestamp()))
                break
            expire_at = datetime.fromtimestamp(
                expire, tz=timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            await asyncio.wait_for(
                http.request(
                    Route(
                        "POST",
                        "/v2/groups/{group_openid}/restrict_chat_setting",
                        group_openid=group_openid,
                    ),
                    json={
                        "members": [
                            {
                                "op": op,
                                "member_openid": member_openid,
                                "mute_expire_at": expire_at,
                            }
                        ]
                    },
                ),
                timeout=5,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("自爆卡车官方禁言失败 user=%s err=%s", member_openid, exc)
            return False

    async def _ban(self, event: AstrMessageEvent, user_id: str, seconds: int) -> str:
        """Mute via Arc Guard; direct mute only when Arc Guard is absent.

        Calls Arc Guard with ``respect_whitelist=False`` so whitelist users
        (e.g. 593405016) can still be muted by the truck. When Arc Guard is
        installed, its verdict is final: a refusal (super-admin protection,
        linked bots, ...) is never bypassed with a direct mute.

        Args:
            event: Current group message event.
            user_id: Platform user id to mute (QQ number or official openid).
            seconds: Extra mute seconds to add.

        Returns:
            Status label: ``ok`` or ``fail``.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return "fail"

        api = self._get_arc_guard_api()
        if api is not None:
            mute = getattr(api, "mute_user", None)
            if callable(mute):
                try:
                    result = await mute(
                        event, uid, int(seconds), respect_whitelist=False
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("自爆卡车弧光护卫禁言异常 user=%s err=%s", uid, exc)
                    return "fail"
                if result.get("ok"):
                    logger.info(
                        "自爆卡车弧光护卫累加禁言 user=%s remaining=%s + add=%s -> total=%s",
                        uid,
                        result.get("remaining_before"),
                        result.get("added"),
                        result.get("total"),
                    )
                    return "ok"
                logger.warning(
                    "自爆卡车弧光护卫禁言未成功 user=%s err=%s",
                    uid,
                    result.get("error") or "unknown",
                )
                return "fail"

        # Arc Guard absent: direct platform-native mute fallback.
        group_id = event.get_group_id()
        bot = getattr(event, "bot", None)
        gid = str(group_id or "")
        if not gid or bot is None:
            return "fail"
        # A qq_official bot (botpy client) exposes ``api._http``; OneBot
        # bots answer any attribute with an action caller that lacks it.
        if hasattr(bot, "api") and hasattr(bot.api, "_http"):
            return "ok" if await self._official_mute(bot, gid, uid, seconds) else "fail"
        if not (gid.isdigit() and uid.isdigit() and hasattr(bot, "set_group_ban")):
            return "fail"
        try:
            await bot.set_group_ban(
                group_id=int(gid),
                user_id=int(uid),
                duration=int(seconds),
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "自爆卡车禁言失败 user=%s duration=%s err=%s", uid, seconds, exc
            )
            return "fail"

    @staticmethod
    def _ban_label(status: str) -> str:
        """Human-readable mute result for reply text."""
        if status == "ok":
            return "已禁言（累加）"
        return "失败（权限不足或接口错误）"

    async def _recent_message_speakers(
        self, event: AstrMessageEvent, count: int
    ) -> list[str]:
        """Speakers of the latest ``count`` non-bot group messages.

        Bot self messages are skipped and do not consume the splash range.
        The same user may appear multiple times if they sent multiple messages.
        The current command sender is also excluded.

        Args:
            event: Current message event.
            count: How many recent human messages to collect.

        Returns:
            Speaker user id list in message order (duplicates kept).
        """
        if count <= 0:
            return []
        group_id = event.get_group_id()
        if not group_id:
            return []
        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id())
        key = f"{event.get_platform_id()}:{group_id}"
        speakers: list[str] = []
        for uid in reversed(self._recent_speakers.get(key, [])):
            if not uid or uid == self_id or uid == sender_id:
                continue
            speakers.append(uid)
            if len(speakers) >= count:
                break
        if speakers:
            return speakers

        bot = getattr(event, "bot", None)
        gid = str(group_id)
        if not bot or not gid.isdigit():
            return []

        # Fetch a wider window so bot replies do not shrink the effective range.
        fetch_count = min(max(int(count) * 8, 20), 50)
        payloads = {
            "group_id": int(gid),
            "message_seq": 0,
            "count": fetch_count,
            "reverseOrder": True,
        }
        try:
            if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                result = await bot.api.call_action("get_group_msg_history", **payloads)
            elif hasattr(bot, "call_action"):
                result = await bot.call_action("get_group_msg_history", **payloads)
            else:
                logger.warning("当前 bot 不支持 get_group_msg_history")
                return []
        except Exception as exc:  # noqa: BLE001
            logger.error("拉取群历史消息失败: %s", exc)
            return []

        messages = result.get("messages", []) if isinstance(result, dict) else []
        self_id = str(event.get_self_id() or "")
        sender_id = str(event.get_sender_id())
        current_msg_id = str(getattr(event.message_obj, "message_id", "") or "")
        speakers: list[str] = []
        for msg in messages:
            if current_msg_id and str(msg.get("message_id", "")) == current_msg_id:
                continue
            uid = str((msg.get("sender") or {}).get("user_id", "") or "")
            # Skip bot self: those messages must not occupy splash slots.
            if not uid or uid == self_id or uid == sender_id:
                continue
            speakers.append(uid)
            if len(speakers) >= count:
                break
        return speakers

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """Record recent human speakers for misfire splash.

        Args:
            event: Incoming group message.
        """
        group_id = event.get_group_id()
        uid = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not group_id or not uid or uid == self_id:
            return
        key = f"{event.get_platform_id()}:{group_id}"
        buf = self._recent_speakers.setdefault(key, [])
        buf.append(uid)
        extra = len(buf) - 50
        if extra > 0:
            del buf[:extra]

    @filter.command("自爆卡车", alias={"selfdestruct", "self_destruct_truck"})
    async def self_destruct_truck(self, event: AstrMessageEvent):
        """自爆卡车：正确对轰 / 空炸波及。

        Args:
            event: 消息事件。
        """
        if not event.get_group_id():
            yield event.plain_result("自爆卡车只能在群聊里用。")
            return

        sender_id = str(event.get_sender_id())
        at_ids = self._collect_at_ids(event)
        # 必须 @ 别人；自己 @ 自己算不合格
        target_id = at_ids[0] if at_ids else ""
        valid = bool(target_id) and target_id != sender_id

        if not valid:
            range_n, chance, splash_sec = self._splash_params()
            splash_hit: list[tuple[str, int]] = []
            splash_fail: list[tuple[str, int]] = []
            splash_rolls = 0
            splash_miss = 0
            # 每条发言单独掷点；同一人可多次命中，禁言秒数按命中次数叠加
            hit_counts: dict[str, int] = {}
            for uid in await self._recent_message_speakers(event, range_n):
                splash_rolls += 1
                if random.random() * 100.0 >= chance:
                    splash_miss += 1
                    continue
                hit_counts[uid] = hit_counts.get(uid, 0) + 1

            for uid, hits in hit_counts.items():
                status = await self._ban(event, uid, splash_sec * hits)
                if status == "ok":
                    splash_hit.append((uid, hits))
                else:
                    splash_fail.append((uid, hits))

            # Self mute = sum of splash mute seconds; fallback random if nobody hit.
            sender_sec = sum(splash_sec * hits for _, hits in splash_hit)
            if sender_sec <= 0:
                misfire_lo, misfire_hi = self._misfire_self_params()
                sender_sec = random.randint(misfire_lo, misfire_hi)
            self_status = await self._ban(event, sender_id, sender_sec)

            # Grade by splash damage vs theoretical max (range_n × splash_sec).
            splash_total = sum(splash_sec * hits for _, hits in splash_hit)
            max_possible = max(1, range_n * splash_sec)
            ratio = splash_total / max_possible
            if splash_total <= 0:
                grade, grade_name = "F", "哑弹"
            elif ratio < 0.25:
                grade, grade_name = "D", "不合格"
            elif ratio < 0.5:
                grade, grade_name = "C", "勉强"
            elif ratio < 0.75:
                grade, grade_name = "B", "不错"
            elif ratio < 1.0:
                grade, grade_name = "A", "优秀"
            else:
                grade, grade_name = "S", "完美波及"

            chain: list = [
                Plain(
                    f"💥 【{grade}·{grade_name}】原地自爆！\n"
                    f"自己炸了 {sender_sec} 秒"
                    f"（{self._ban_label(self_status)}；"
                    f"{'波及禁言总和' if splash_hit else '无人波及，随机自罚'}）\n"
                    f"评级依据：波及 {splash_total}/{max_possible} 秒"
                    f"（邻近 {range_n} 条 × {chance:g}% × {splash_sec} 秒/次）"
                ),
            ]
            if splash_hit:
                chain.append(Plain("\n波及者：\n"))
                for uid, hits in splash_hit:
                    chain.append(At(qq=uid))
                    chain.append(Plain(f"：波及{hits}次禁言{splash_sec * hits}秒\n"))
            if splash_fail:
                chain.append(Plain("波及失败：\n"))
                for uid, hits in splash_fail:
                    chain.append(At(qq=uid))
                    chain.append(Plain(f"：波及{hits}次\n"))
            if not splash_hit and not splash_fail:
                chain.append(
                    Plain(
                        "\n无人被波及"
                        + (
                            f"（掷点 {splash_rolls} 次，未命中 {splash_miss} 次）"
                            if splash_rolls
                            else ""
                        )
                    )
                )
            # use_markdown_ stays at its default (follow the platform): on
            # qq_official an ``At`` only renders as a real mention in markdown.
            yield event.chain_result(chain)
            event.stop_event()
            return

        min_damage, max_damage, total_pool = self._truck_params()
        sender_sec = random.randint(min_damage, max_damage)
        target_sec = total_pool - sender_sec
        target_status = await self._ban(event, target_id, target_sec)
        sender_status = await self._ban(event, sender_id, sender_sec)

        # Grade by how much of the pool landed on the target.
        target_share = target_sec / max(1, total_pool)
        if target_share >= 0.75:
            grade, grade_name = "S", "致命对轰"
        elif target_share >= 0.6:
            grade, grade_name = "A", "强势对轰"
        elif target_share >= 0.45:
            grade, grade_name = "B", "均势对轰"
        elif target_share >= 0.3:
            grade, grade_name = "C", "吃亏对轰"
        else:
            grade, grade_name = "D", "惨烈自损"

        result = event.chain_result(
            [
                Plain(
                    f"💥 【{grade}·{grade_name}】自爆卡车发动！\n"
                    f"发起者伤害 {sender_sec} 秒 → 对方 {target_sec} 秒（累加禁言）\n"
                    "发起者 "
                ),
                At(qq=sender_id),
                Plain(f"：{self._ban_label(sender_status)}\n被@者 "),
                At(qq=target_id),
                Plain(f"：{self._ban_label(target_status)}"),
            ]
        )
        yield result
        event.stop_event()
