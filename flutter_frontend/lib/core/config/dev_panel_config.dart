import 'package:flutter/material.dart';

enum DevPanelType { toggle, dropdown, textInput }

class DevPanelItem {
  final String key;
  final String label;
  final String? subtitle;
  final DevPanelType type;
  final dynamic defaultValue;
  final List<String>? options;
  final IconData? icon;
  final Color? iconColor;

  const DevPanelItem({
    required this.key,
    required this.label,
    this.subtitle,
    required this.type,
    required this.defaultValue,
    this.options,
    this.icon,
    this.iconColor,
  });
}

class DevPanelConfig {
  static const String devModeKey = 'dev_mode';
  static const String llmModelKey = 'llm_model';

  // 可用模型列表（DeepSeek 优先，豆包备灾）
  static const List<DevPanelItem> panels = [
    DevPanelItem(
      key: llmModelKey,
      label: 'LLM 模型',
      subtitle: '调试用：强制使用指定模型（不走容灾）',
      type: DevPanelType.dropdown,
      defaultValue: 'deepseek-chat',
      options: ['deepseek-chat', 'doubao-pro'],
      icon: Icons.smart_toy_outlined,
      iconColor: Colors.purple,
    ),
  ];
}
