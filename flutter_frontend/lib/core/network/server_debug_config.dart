// 服务器地址配置（支持运行时动态切换）
// 使用 SharedPreferences 持久化用户选择

import 'package:shared_preferences/shared_preferences.dart';

/// 预设服务器枚举
enum ServerPreset {
  local,   // 本地默认：172.20.10.4
  remote,  // 远端：20.2.80.19
  custom,  // 自定义
}

/// 默认值常量（编译时常量）
const String kDefaultLocalHost = '172.20.10.4';
const String kDefaultRemoteHost = '20.2.80.19';
const int kDefaultPort = 8000;

/// SharedPreferences keys
const String _keyServerPreset = 'server_preset';
const String _keyCustomHost = 'custom_server_host';
const String _keyCustomPort = 'custom_server_port';

/// 获取当前选中的服务器预设
Future<ServerPreset> getServerPreset() async {
  final prefs = await SharedPreferences.getInstance();
  final index = prefs.getInt(_keyServerPreset) ?? 0;
  return ServerPreset.values[index.clamp(0, ServerPreset.values.length - 1)];
}

/// 设置服务器预设
Future<void> setServerPreset(ServerPreset preset) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setInt(_keyServerPreset, preset.index);
}

/// 获取自定义服务器地址
Future<String?> getCustomHost() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getString(_keyCustomHost);
}

Future<int?> getCustomPort() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getInt(_keyCustomPort);
}

/// 设置自定义服务器地址
Future<void> setCustomServer(String host, int port) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(_keyCustomHost, host);
  await prefs.setInt(_keyCustomPort, port);
}

/// 运行时获取当前生效的服务器 Host
Future<String> getCurrentHost() async {
  final preset = await getServerPreset();
  switch (preset) {
    case ServerPreset.local:
      return kDefaultLocalHost;
    case ServerPreset.remote:
      return kDefaultRemoteHost;
    case ServerPreset.custom:
      return await getCustomHost() ?? kDefaultLocalHost;
  }
}

/// 运行时获取当前生效的服务器 Port
Future<int> getCurrentPort() async {
  final preset = await getServerPreset();
  switch (preset) {
    case ServerPreset.local:
    case ServerPreset.remote:
      return kDefaultPort;
    case ServerPreset.custom:
      return await getCustomPort() ?? kDefaultPort;
  }
}
