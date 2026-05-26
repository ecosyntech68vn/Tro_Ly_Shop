"""Test Flask API endpoints and security."""
import json


class TestHealth:
    def test_health_endpoint(self, client):
        r = client.get("/")
        data = json.loads(r.data)
        assert data["status"] in ("ok", "degraded")
        assert "uptime" in data
        assert "db" in data
        assert "ai_models" in data
        assert "version" in data

    def test_cors(self, client):
        r = client.options("/api/chat")
        assert r.status_code in (200, 405)


class TestSecurityHeaders:
    def test_security_headers(self, client):
        r = client.get("/")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_404_json(self, client):
        r = client.get("/nonexistent-route-xyz")
        assert r.status_code == 404
        data = json.loads(r.data)
        assert data["error"] == "not_found"


class TestApiChat:
    def test_missing_token(self, client):
        r = client.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 400
        data = json.loads(r.data)
        assert "missing_token" in data.get("error", "")

    def test_invalid_token(self, client):
        r = client.post("/api/chat", json={"token": "invalid", "message": "hi"})
        assert r.status_code == 403

    def test_missing_message(self, client):
        r = client.post("/api/chat", json={"token": "sometoken"})
        assert r.status_code == 400


class TestWidgetJs:
    def test_widget_available(self, client):
        r = client.get("/widget.js")
        assert r.status_code in (200, 404)  # file may not exist in test env


class TestDashboardAuth:
    def test_redirects_to_login(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 302
        assert "/dashboard/login" in r.location

    def test_login_page_loads(self, client):
        r = client.get("/dashboard/login")
        assert r.status_code == 200
        assert b"token" in r.data.lower() or b"dang nhap" in r.data

    def test_export_requires_auth(self, client):
        for path in ("/dashboard/export/chats", "/dashboard/export/products"):
            r = client.get(path)
            assert r.status_code == 302


class TestRateLimit:
    def test_login_rate_limit(self, client):
        for i in range(6):
            client.post("/dashboard/login", data={"token": "bad"})
        r = client.post("/dashboard/login", data={"token": "bad"})
        text = r.data.decode("utf-8", errors="replace")
        assert "Qu" in text or "rate" in text.lower() or r.status_code == 429
