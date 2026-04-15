# EnglishCoach

Flutter client (`flutter_frontend`) and Python FastAPI backend (`python_backend`) for an English AI coaching app.

## Quick start

**Backend**

```bash
cd python_backend
cp config.env.example config.env
# Edit config.env with your API keys, then:
pip install -r requirements.txt
python server.py
```

**Frontend**

```bash
cd flutter_frontend
flutter pub get
flutter run
```

Point the app WebSocket URL at your running backend (see `websocket_client.dart` / settings as applicable).
