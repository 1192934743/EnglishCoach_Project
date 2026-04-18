// 后端 HTTP / WebSocket 共用主机与端口。
// 真机调试：改为与你运行 `uvicorn` 的电脑在同一局域网的 IP（本机 ipconfig 查看）。
// 若此处与后端不一致，会看到旧数据或英文标题（连到旧服务/空 title_zh）。
const String kBackendHost = '172.20.10.4';
const int kBackendPort = 8000;

String get kBackendHttpBase => 'http://$kBackendHost:$kBackendPort';
String get kBackendWsUrl => 'ws://$kBackendHost:$kBackendPort/ws/coach';
