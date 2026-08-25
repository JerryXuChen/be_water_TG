from __future__ import annotations

import logging

from src.message_loader import load_messages, validate_messages
from src.selector import MessageSelector

logger = logging.getLogger(__name__)


class MessageManager:
    """管理多个群组的消息选择器，每个群组拥有独立的 MessageSelector 实例。
    同一文件被多个群组引用时，只加载一次无需重复处理。
    """

    def __init__(self, group_file_map: dict[str, str]) -> None:
        """初始化消息管理器。

        Args:
            group_file_map: 群组链接到消息文件路径的映射。
                例如: {"https://t.me/group1": "messages1.txt", "https://t.me/group2": "messages2.txt"}

        Raises:
            ValueError: group_file_map 为空，或消息文件不包含有效消息时抛出。
            FileNotFoundError: 消息文件不存在时抛出。
        """
        if not group_file_map:
            raise ValueError("群组映射不能为空")

        self._selectors: dict[str, MessageSelector] = {}
        loaded_files: dict[str, MessageSelector] = {}  # 缓存已加载的 selector

        for group, file_path in group_file_map.items():
            if file_path in loaded_files:
                self._selectors[group] = loaded_files[file_path]
            else:
                messages = load_messages(file_path)
                validate_messages(messages)
                selector = MessageSelector(messages)
                self._selectors[group] = selector
                loaded_files[file_path] = selector
                logger.info("已加载 %d 条消息从: %s", len(messages), file_path)

    def get_message(self, group: str) -> str:
        """获取指定群组的下一条消息。

        通过该群组的 MessageSelector 随机选择一条消息，
        确保不与上一次发送的消息相同（单条消息时例外）。

        Args:
            group: 群组链接。

        Returns:
            选中的消息字符串。

        Raises:
            KeyError: 指定群组未在管理器中注册时抛出。
        """
        if group not in self._selectors:
            raise KeyError(f"群组未注册: {group}")

    def load_loose_messages(self, file_path: str) -> list[str]:
        """加载宽松群语料库（按行，忽略 # 注释与空行）。

        Args:
            file_path: 语料库文件路径。

        Returns:
            有效语料行列表。
        """
        import os

        if not os.path.exists(file_path):
            logger.warning("宽松群语料库不存在: %s", file_path)
            return []
        with open(file_path, "r", encoding="utf-8") as handle:
            lines = [
                ln.strip()
                for ln in handle
                if ln.strip() and not ln.strip().startswith("#")
            ]
        logger.info("已加载 %d 条宽松群语料从: %s", len(lines), file_path)
        return lines
        return self._selectors[group].select()


def load_message_files(group_file_map: dict[str, str]) -> MessageManager:
    """创建并返回一个 MessageManager 实例。

    便捷工厂函数，等效于直接调用 MessageManager(group_file_map)。

    Args:
        group_file_map: 群组链接到消息文件路径的映射。

    Returns:
        配置完成的 MessageManager 实例。
    """
    return MessageManager(group_file_map)
