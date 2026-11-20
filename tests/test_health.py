"""The keep-alive endpoint: reachable without a token, and it touches nothing."""


def test_health_answers_without_a_token(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_not_under_auth(client):
    """A monitor has no credentials, so the URL must stay public."""
    assert client.get("/health", headers={"Authorization": "Bearer nonsense"}).status_code == 200
