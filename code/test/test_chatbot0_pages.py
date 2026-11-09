"""chatbot0 public pages (SMA-8): they render, carry the right links and SEO,
and leave the existing routes ("/" Swagger UI, /health, the API) alone."""

import pytest

from chatbot0 import content


@pytest.mark.parametrize("path", ["/chatbot0/", "/chatbot0/pricing"])
def test_public_pages_render(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert res.mimetype == "text/html"
    html = res.get_data(as_text=True)
    assert 'chatbot<span class="pub-wordmark__zero">0</span>' in html
    assert content.SIGNUP_URL in html
    assert 'href="static/css/chatbot0.css"' in html
    assert "© 2026 madhyamakist pvt ltd" in html or "&copy; 2026 madhyamakist pvt ltd" in html
    assert "sales0" not in html.lower()


def test_landing_seo(client):
    html = client.get("/chatbot0/").get_data(as_text=True)
    assert "<title>chatbot0, AI chat agent for small business websites</title>" in html
    assert '<link rel="canonical" href="https://chatbot0.smalltech.in/">' in html
    assert 'property="og:url" content="https://chatbot0.smalltech.in/"' in html
    assert content.ZER0_URL in html


def test_pricing_shows_every_plan(client):
    html = client.get("/chatbot0/pricing").get_data(as_text=True)
    assert '<link rel="canonical" href="https://chatbot0.smalltech.in/pricing">' in html
    for plan in content.PLANS:
        assert f"${plan.monthly}<span>/month</span>" in html
        if plan.yearly:
            assert f"or ${plan.yearly}/month billed yearly (save {plan.yearly_saving_pct}%)" in html


def test_yearly_savings_are_computed_from_prices():
    by_key = {p.key: p for p in content.PLANS}
    assert by_key["free"].yearly_saving_pct is None
    assert by_key["basic"].yearly_saving_pct == 25  # $4 -> $3
    assert by_key["pro"].yearly_saving_pct == 17  # $12 -> $10


def test_robots_and_sitemap(client):
    robots = client.get("/chatbot0/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://chatbot0.smalltech.in/sitemap.xml" in robots.get_data(as_text=True)
    sitemap = client.get("/chatbot0/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.mimetype == "application/xml"
    body = sitemap.get_data(as_text=True)
    assert "<loc>https://chatbot0.smalltech.in/</loc>" in body
    assert "<loc>https://chatbot0.smalltech.in/pricing</loc>" in body


@pytest.mark.parametrize(
    "asset",
    ["css/chatbot0.css", "img/icon.svg", "img/favicon.ico", "img/apple-icon.png",
     "img/logo-smalltech.svg", "img/og-chatbot0.png"],
)
def test_static_assets_served(client, asset):
    assert client.get(f"/chatbot0/static/{asset}").status_code == 200


def test_prefix_without_slash_redirects_to_landing(client):
    res = client.get("/chatbot0")
    assert res.status_code in (301, 308)
    assert res.headers["Location"].endswith("/chatbot0/")


def test_root_is_still_the_swagger_ui(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "swagger" in html.lower()
    assert "chatbot0" not in html


HOST = {"Host": "chatbot0.smalltech.in"}


@pytest.mark.parametrize("path", ["/", "/pricing"])
def test_chatbot0_host_serves_pages_at_root(client, path):
    res = client.get(path, headers=HOST)
    assert res.status_code == 200 and res.mimetype == "text/html"
    assert 'chatbot<span class="pub-wordmark__zero">0</span>' in res.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/static/css/chatbot0.css", "/static/img/icon.svg", "/robots.txt", "/sitemap.xml"])
def test_chatbot0_host_serves_assets_at_root(client, path):
    assert client.get(path, headers=HOST).status_code == 200


def test_chatbot0_host_leaves_api_paths_alone(client):
    assert client.get("/health", headers=HOST).status_code == client.get("/health").status_code


def test_other_hosts_keep_swagger_at_root(client):
    html = client.get("/", headers={"Host": "zero-sales-agent-a23rjysfbq-uc.a.run.app"}).get_data(as_text=True)
    assert "chatbot<span" not in html
