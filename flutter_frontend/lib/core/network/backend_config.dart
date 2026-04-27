// 后端 HTTP / WebSocket 共用主机与端口。
// 真机调试：改为与你运行 `uvicorn` 的电脑在同一局域网的 IP（本机 ipconfig 查看）。
// 若此处与后端不一致，会看到旧数据或英文标题（连到旧服务/空 title_zh）。
//
// 支持运行时动态切换：172.20.10.2（本地）、20.2.80.19（远端）、自定义
// 详见 server_debug_config.dart

import 'server_debug_config.dart';

// ── 同步缓存（启动时由 App 初始化）───────────────────────────────────────────
String _cachedHost = kDefaultLocalHost;
int _cachedPort = kDefaultPort;

/// 启动时调用：加载持久化配置并缓存
Future<void> initServerConfig() async {
  _cachedHost = await getCurrentHost();
  _cachedPort = await getCurrentPort();
}

/// 运行时切换 preset：更新缓存并返回是否成功
Future<bool> switchServerPreset(
  ServerPreset preset, {
  String? customHost,
  int? customPort,
}) async {
  try {
    if (preset == ServerPreset.custom) {
      if (customHost == null || customPort == null) return false;
      await setCustomServer(customHost, customPort);
    }
    await setServerPreset(preset);
    _cachedHost = await getCurrentHost();
    _cachedPort = await getCurrentPort();
    return true;
  } catch (_) {
    return false;
  }
}

String get kBackendHost => _cachedHost;
int get kBackendPort => _cachedPort;

String get kBackendHttpBase => 'http://$kBackendHost:$kBackendPort';
String get kBackendWsUrl => 'ws://$kBackendHost:$kBackendPort/ws/coach';
