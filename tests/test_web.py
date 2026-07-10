import re


def _token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def test_login_page_ok(client):
    assert client.get("/login").status_code == 200


def test_anon_admin_redirects(client):
    r = client.get("/admin")
    assert r.status_code in (301, 302)
    assert "/login" in r.headers["Location"]


def test_post_without_csrf_rejected(client):
    r = client.post("/login", data={"role": "admin"})
    assert r.status_code == 400


def test_admin_login_with_csrf(client):
    token = _token(client)
    r = client.post("/login", data={"role": "admin", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 302
    assert "/admin" in r.headers["Location"]


def test_admin_pin_enforced(monkeypatch, db):
    import importlib
    monkeypatch.setenv("ADMIN_PIN", "1234")
    import app as app_module
    importlib.reload(app_module)
    c = app_module.create_app().test_client()
    token = _token(c)
    # wrong pin -> bounced back to login
    r = c.post("/login", data={"role": "admin", "pin": "0000", "csrf_token": token})
    assert "/login" in r.headers["Location"]
    token = _token(c)
    r = c.post("/login", data={"role": "admin", "pin": "1234", "csrf_token": token})
    assert "/admin" in r.headers["Location"]
