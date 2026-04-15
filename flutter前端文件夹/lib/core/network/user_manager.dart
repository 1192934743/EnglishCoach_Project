import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

class UserManager {
  static const String _uuidKey = 'app_user_uuid';
  static String? _currentUuid;

  // 初始化并获取 UUID
  static Future<String> getOrCreateUuid() async {
    // 如果内存里已经有了，直接返回，避免频繁读本地文件
    if (_currentUuid != null) return _currentUuid!;

    final prefs = await SharedPreferences.getInstance();
    _currentUuid = prefs.getString(_uuidKey);

    // 如果是第一次打开 App，本地没有存 UUID
    if (_currentUuid == null) {
      // 悄悄生成一个新的隐形身份证！
      _currentUuid = const Uuid().v4();
      await prefs.setString(_uuidKey, _currentUuid!);
      debugPrint("🌟 [UserManager] 首次打开，已生成隐形 UUID: $_currentUuid");
    } else {
      debugPrint("👋 [UserManager] 欢迎回来，当前 UUID: $_currentUuid");
    }

    return _currentUuid!;
  }
}
