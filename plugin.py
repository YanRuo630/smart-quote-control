from maibot_sdk import MaiBotPlugin, PluginConfigBase, Field, HookHandler
from maibot_sdk.types import HookMode, HookOrder, ErrorPolicy
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple


class PluginSection(PluginConfigBase):
    """智能引用回复控制基础设置"""
    __ui_label__ = "基础设置"

    config_version: str = Field(
        default="1.0.0",
        description="配置文件版本号",
    )

    enabled: bool = Field(
        default=True,
        description="是否启用智能引用回复控制插件",
    )

    # 私聊设置
    disable_quote_in_private: bool = Field(
        default=True,
        description="私聊环境下禁用引用回复，让一对一对话更自然",
    )

    # 群聊智能引用设置
    enable_smart_quote_in_group: bool = Field(
        default=True,
        description="群聊中根据消息密度和回复延迟智能判断是否需要引用",
    )

    group_message_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="消息数量阈值：艾特后到回复前的消息数达到此值时才引用（范围 1-20）",
    )

    group_time_window: int = Field(
        default=60,
        ge=10,
        le=300,
        description="时间窗口：统计消息的时间范围，单位秒（范围 10-300）",
    )

    reply_delay_threshold: int = Field(
        default=30,
        ge=5,
        le=120,
        description="延迟阈值：回复延迟超过此时间（秒）则强制引用，防止用户忘记上下文（范围 5-120）",
    )

    # 高级设置
    force_quote_when_at: bool = Field(
        default=False,
        description="被艾特时强制引用回复，无视消息数和延迟判断",
    )

    whitelist_groups: str = Field(
        default="",
        description="白名单群号：这些群始终启用引用回复，多个群号用英文逗号分隔，留空表示应用到所有群",
        json_schema_extra={"placeholder": "例如: 123456,789012"},
    )

    blacklist_groups: str = Field(
        default="",
        description="黑名单群号：这些群始终禁用引用回复，多个群号用英文逗号分隔",
        json_schema_extra={"placeholder": "例如: 123456,789012"},
    )


class SmartQuoteConfig(PluginConfigBase):
    """智能引用回复控制插件完整配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)


class SmartQuoteControlPlugin(MaiBotPlugin):
    config_model = SmartQuoteConfig

    def __init__(self):
        super().__init__()
        # 记录每个群的消息历史 {group_id: [(timestamp, message_id), ...]}
        self._group_message_history: Dict[str, List[Tuple[datetime, str]]] = defaultdict(list)
        # 记录艾特时间 {stream_id: timestamp}
        self._at_timestamps: Dict[str, datetime] = {}

    async def on_load(self) -> None:
        self.ctx.logger.info("智能引用回复控制插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("智能引用回复控制插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self.ctx.logger.info("智能引用回复控制插件配置已更新")

    def _is_private_chat(self, message: dict) -> bool:
        """判断消息是否为私聊"""
        if message.get("is_private") is True:
            return True

        chat_type = (
            message.get("chat_type")
            or message.get("scene")
            or message.get("detail_type")
            or ""
        )
        if chat_type in ("private", "direct"):
            return True

        add_config = {}
        if "message_info" in message:
            add_config = message.get("message_info", {}).get("additional_config") or {}
        elif "additional_config" in message:
            add_config = message.get("additional_config") or {}

        napcat_type = (
            add_config.get("napcat_message_type")
            or add_config.get("napcat_notice_type")
            or ""
        )
        if napcat_type == "private":
            return True

        if add_config.get("platform_io_target_user_id") is not None and not message.get("group_id"):
            return True

        return False

    def _extract_group_id(self, message: dict) -> str:
        """从消息中提取群组 ID"""
        group_id = message.get("group_id") or ""
        if group_id:
            return str(group_id)

        msg_info = message.get("message_info") or {}
        group_info = msg_info.get("group_info") or {}
        if group_info:
            gid = group_info.get("group_id") or group_info.get("id")
            if gid:
                return str(gid)

        return ""

    def _is_at_bot(self, message: dict) -> bool:
        """判断消息是否艾特了 bot"""
        return bool(
            message.get("is_at")
            or message.get("at_me")
            or message.get("is_mentioned")
        )

    def _parse_id_list(self, raw_str: str) -> List[str]:
        """解析逗号分隔的 ID 列表"""
        if not raw_str:
            return []
        return [item.strip() for item in raw_str.split(",") if item.strip()]

    def _clean_old_messages(self, group_id: str, current_time: datetime) -> None:
        """清理超出时间窗口的旧消息记录"""
        time_window = timedelta(seconds=self.config.plugin.group_time_window)
        cutoff_time = current_time - time_window

        if group_id in self._group_message_history:
            self._group_message_history[group_id] = [
                (ts, msg_id)
                for ts, msg_id in self._group_message_history[group_id]
                if ts > cutoff_time
            ]

    def _count_recent_messages(self, group_id: str, since_time: datetime) -> int:
        """统计指定时间后的消息数量（不包括艾特消息本身）"""
        if group_id not in self._group_message_history:
            return 0

        # 统计所有在 since_time 之后的消息
        recent_messages = [
            (ts, msg_id) for ts, msg_id in self._group_message_history[group_id]
            if ts > since_time
        ]

        # 调试：输出统计到的消息
        if recent_messages:
            self.ctx.logger.debug(
                f"[_count_recent_messages] group_id={group_id}, "
                f"since_time={since_time}, "
                f"统计到 {len(recent_messages)} 条消息: {[(ts.strftime('%H:%M:%S.%f'), msg_id[:8]) for ts, msg_id in recent_messages]}"
            )

        return len(recent_messages)

    def _should_quote_in_group(self, group_id: str, stream_id: str, is_at: bool) -> bool:
        """判断群聊中是否应该引用回复"""
        # 检查黑白名单
        whitelist = self._parse_id_list(self.config.plugin.whitelist_groups)
        blacklist = self._parse_id_list(self.config.plugin.blacklist_groups)

        if blacklist and group_id in blacklist:
            self.ctx.logger.debug(f"群 {group_id} 在黑名单中，禁用引用")
            return False

        if whitelist and group_id in whitelist:
            self.ctx.logger.debug(f"群 {group_id} 在白名单中，强制引用")
            return True

        # 如果配置了被艾特时强制引用
        if is_at and self.config.plugin.force_quote_when_at:
            self.ctx.logger.debug("被艾特且配置强制引用")
            return True

        # 如果未启用智能判断，使用默认行为（MaiBot 原生逻辑）
        if not self.config.plugin.enable_smart_quote_in_group:
            return True

        # 智能判断：检查艾特时间后的消息数量和回复延迟
        if stream_id not in self._at_timestamps:
            self.ctx.logger.warning(f"未找到艾特时间记录 stream_id={stream_id}，兜底：检查最近消息数")
            # 兜底逻辑：如果没有艾特时间记录，统计最近时间窗口内的消息数
            current_time = datetime.now()
            time_window = timedelta(seconds=self.config.plugin.group_time_window)
            recent_time = current_time - time_window
            message_count = self._count_recent_messages(group_id, recent_time)
            threshold = self.config.plugin.group_message_threshold
            should_quote = message_count >= threshold

            self.ctx.logger.info(
                f"群 {group_id} 兜底判断: 最近{self.config.plugin.group_time_window}秒消息数={message_count}, "
                f"阈值={threshold}, 是否引用={should_quote}"
            )
            return should_quote

        at_time = self._at_timestamps[stream_id]
        current_time = datetime.now()

        # 计算回复延迟（秒）
        reply_delay = (current_time - at_time).total_seconds()
        delay_threshold = self.config.plugin.reply_delay_threshold

        # 判断1：回复延迟超过阈值 -> 强制引用
        if reply_delay >= delay_threshold:
            self.ctx.logger.info(
                f"群 {group_id} 回复延迟判断: 延迟={reply_delay:.1f}秒, "
                f"阈值={delay_threshold}秒, 超时强制引用"
            )
            return True

        # 判断2：消息数量超过阈值 -> 引用
        message_count = self._count_recent_messages(group_id, at_time)
        message_threshold = self.config.plugin.group_message_threshold
        should_quote = message_count >= message_threshold

        self.ctx.logger.info(
            f"群 {group_id} 智能判断: 艾特后消息数={message_count}, 阈值={message_threshold}, "
            f"回复延迟={reply_delay:.1f}秒, 延迟阈值={delay_threshold}秒, 是否引用={should_quote}"
        )

        return should_quote

    @HookHandler(
        "chat.receive.before_process",
        name="record_group_message",
        description="记录群消息用于智能引用判断",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def record_group_message(self, **kwargs):
        """记录群消息和艾特时间"""
        # 强制输出日志，确保能看到
        self.ctx.logger.info(f"========== [record_group_message] Hook 被触发 ==========")

        if not self.config.plugin.enabled:
            self.ctx.logger.info(f"[record_group_message] 插件未启用，跳过")
            return {"action": "continue", "modified_kwargs": kwargs}

        message = kwargs.get("message", {})
        if not message:
            self.ctx.logger.info(f"[record_group_message] 消息为空，跳过")
            return {"action": "continue", "modified_kwargs": kwargs}

        self.ctx.logger.info(f"[record_group_message] 收到消息，类型: {type(message)}, 字段: {list(message.keys()) if isinstance(message, dict) else 'not dict'}")

        # 只处理群聊消息
        if self._is_private_chat(message):
            self.ctx.logger.info(f"[record_group_message] 私聊消息，跳过")
            return {"action": "continue", "modified_kwargs": kwargs}

        group_id = self._extract_group_id(message)
        if not group_id:
            self.ctx.logger.info(f"[record_group_message] 未提取到群号，跳过")
            return {"action": "continue", "modified_kwargs": kwargs}

        # 记录消息时间
        current_time = datetime.now()
        message_id = str(message.get("message_id", ""))

        if message_id:
            self._group_message_history[group_id].append((current_time, message_id))
            self._clean_old_messages(group_id, current_time)
            self.ctx.logger.info(f"[record_group_message] 记录消息: group_id={group_id}, message_id={message_id}")

        # 如果被艾特，记录艾特时间
        is_at = self._is_at_bot(message)
        stream_id = str(message.get("stream_id", ""))

        self.ctx.logger.info(
            f"[record_group_message] 检查艾特: group_id={group_id}, stream_id={stream_id}, is_at={is_at}"
        )

        if is_at:
            if stream_id:
                self._at_timestamps[stream_id] = current_time
                self.ctx.logger.info(f"✓✓✓ 记录艾特时间: stream_id={stream_id}, group_id={group_id}, time={current_time} ✓✓✓")
            else:
                self.ctx.logger.warning(f"✗ 被艾特但 stream_id 为空: group_id={group_id}")

        return {"action": "continue", "modified_kwargs": kwargs}

    def _has_reply_component(self, message: dict) -> bool:
        """检查消息中是否已经包含引用回复组件"""
        raw_message = message.get("raw_message", [])
        if not isinstance(raw_message, list):
            return False

        for component in raw_message:
            if isinstance(component, dict):
                component_type = str(component.get("type", "")).lower()
                if component_type == "reply":
                    return True
        return False

    def _remove_reply_component(self, message: dict) -> dict:
        """从消息中移除引用回复组件"""
        raw_message = message.get("raw_message", [])
        if not isinstance(raw_message, list):
            return message

        filtered_components = [
            component for component in raw_message
            if not (isinstance(component, dict) and str(component.get("type", "")).lower() == "reply")
        ]

        updated_message = dict(message)
        updated_message["raw_message"] = filtered_components
        return updated_message

    @HookHandler(
        "send_service.after_build_message",
        name="control_quote_behavior",
        description="根据聊天环境控制引用回复行为",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,  # 改为 LATE，在智能分段之后执行
        error_policy=ErrorPolicy.SKIP,
    )
    async def control_quote_behavior(self, **kwargs):
        """控制引用回复行为"""
        if not self.config.plugin.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}

        message = kwargs.get("message", {})
        stream_id = str(kwargs.get("stream_id", ""))
        set_reply = kwargs.get("set_reply", False)
        reply_message = kwargs.get("reply_message")

        # 尝试从 reply_message 获取艾特时间（兜底机制）
        if reply_message and stream_id and stream_id not in self._at_timestamps:
            is_at = False
            if hasattr(reply_message, "is_at"):
                is_at = bool(reply_message.is_at)
            elif hasattr(reply_message, "at_me"):
                is_at = bool(reply_message.at_me)
            elif hasattr(reply_message, "is_mentioned"):
                is_at = bool(reply_message.is_mentioned)

            if is_at:
                # 获取消息时间
                message_time = datetime.now()
                if hasattr(reply_message, "timestamp"):
                    try:
                        if isinstance(reply_message.timestamp, datetime):
                            message_time = reply_message.timestamp
                    except:
                        pass

                self._at_timestamps[stream_id] = message_time
                self.ctx.logger.info(
                    f"✓ [after_build_message兜底] 记录艾特时间: stream_id={stream_id}, time={message_time}"
                )

        if not message:
            return {"action": "continue", "modified_kwargs": kwargs}

        # 检查是否有引用回复（无论是通过 set_reply 参数还是消息中的 ReplyComponent）
        has_reply = set_reply or self._has_reply_component(message)

        if not has_reply:
            self.ctx.logger.debug(f"消息没有引用回复，跳过: stream_id={stream_id}")
            return {"action": "continue", "modified_kwargs": kwargs}

        self.ctx.logger.debug(f"检查引用回复: stream_id={stream_id}, set_reply={set_reply}, has_component={self._has_reply_component(message)}")

        # 判断是私聊还是群聊
        is_private = self._is_private_chat(message)

        should_remove_quote = False

        if is_private:
            # 私聊处理
            if self.config.plugin.disable_quote_in_private:
                self.ctx.logger.info(f"私聊禁用引用回复: stream_id={stream_id}")
                should_remove_quote = True
        else:
            # 群聊处理
            group_id = self._extract_group_id(message)
            is_at = self._is_at_bot(message)

            if not self._should_quote_in_group(group_id, stream_id, is_at):
                self.ctx.logger.info(f"群聊智能判断禁用引用回复: group_id={group_id}, stream_id={stream_id}")
                should_remove_quote = True
            else:
                self.ctx.logger.info(f"群聊保持引用回复: group_id={group_id}, stream_id={stream_id}")

        if should_remove_quote:
            # 移除引用组件并更新参数
            kwargs["set_reply"] = False
            kwargs["message"] = self._remove_reply_component(message)

        return {"action": "continue", "modified_kwargs": kwargs}


def create_plugin():
    return SmartQuoteControlPlugin()
