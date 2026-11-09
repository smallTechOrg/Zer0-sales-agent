"""chatbot0 public pages (Linear SMA-8), rendered server-side by Flask.

chatbot0 is the AI chat agent this backend powers. Its marketing pages live
here, under the /chatbot0/ prefix, because "/" already serves the legacy
Swagger UI (api/swagger.py) and must keep doing so.

    /chatbot0/             landing page
    /chatbot0/pricing      pricing
    /chatbot0/robots.txt   for when chatbot0.smalltech.in maps onto this prefix
    /chatbot0/sitemap.xml
    /chatbot0/static/...   CSS, favicons, logo (files in static/chatbot0/)

Links and asset paths in the templates are relative ("pricing", "./",
"static/..."), so the same HTML works at /chatbot0/ on this service and at the
root of chatbot0.smalltech.in. On that host (Cloud Run domain mapping, see
register_host_routing) "/", "/pricing", "/robots.txt", "/sitemap.xml" and
"/static/..." serve these pages; every other path, and every other host, is
untouched, so the API and the Swagger UI behave exactly as before. Canonical, Open Graph and sitemap URLs always point at
https://chatbot0.smalltech.in.

The pages take no input, read nothing from the database and call no LLM.
"""

from __future__ import annotations

import os

from flask import Blueprint, Flask, Response, render_template, request, send_from_directory

from . import content

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "chatbot0")

chatbot0_bp = Blueprint(
    "chatbot0",
    __name__,
    url_prefix="/chatbot0",
    static_folder=_STATIC_DIR,
    static_url_path="/static",
)

LANDING_TITLE = "chatbot0, AI chat agent for small business websites"
LANDING_DESCRIPTION = (
    "chatbot0 sits on your website, answers visitors, captures leads and hands you the warm conversation. "
    "Free plan with 200 conversations a month. No code. By smallTech."
)
PRICING_TITLE = "Pricing, chatbot0"
PRICING_DESCRIPTION = (
    f"Simple pricing for chatbot0. Start free with {content.FREE_CONVERSATIONS} conversations a month, "
    f"upgrade from ${content.LOWEST_PAID_MONTHLY} a month. No hidden fees."
)


def _render(template: str, *, path: str, title: str, description: str) -> str:
    return render_template(
        template,
        page_path=path,
        page_title=title,
        page_description=description,
        canonical=content.SITE_URL + path,
        site_url=content.SITE_URL,
        signup_url=content.SIGNUP_URL,
        zer0_url=content.ZER0_URL,
        contact_email=content.CONTACT_EMAIL,
        github_url=content.GITHUB_URL,
        linkedin_url=content.LINKEDIN_URL,
        plans=content.PLANS,
        free_conversations=content.FREE_CONVERSATIONS,
        max_yearly_saving_pct=content.MAX_YEARLY_SAVING_PCT,
        # The chat widget needs a `domains` row for the page's origin, so it is only rendered on the chatbot0 host.
        chat_widget=(request.host or "").split(":")[0].lower() in _chat_hosts(),
        widget_url=content.WIDGET_URL,
    )


@chatbot0_bp.route("/", methods=["GET"])
def landing():
    return _render("chatbot0/landing.html", path="/", title=LANDING_TITLE, description=LANDING_DESCRIPTION)


@chatbot0_bp.route("/pricing", methods=["GET"])
def pricing():
    return _render("chatbot0/pricing.html", path="/pricing", title=PRICING_TITLE, description=PRICING_DESCRIPTION)


@chatbot0_bp.route("/robots.txt", methods=["GET"])
def robots():
    body = f"User-agent: *\nAllow: /\nSitemap: {content.SITE_URL}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@chatbot0_bp.route("/sitemap.xml", methods=["GET"])
def sitemap():
    urls = "".join(f"<url><loc>{content.SITE_URL}{p}</loc></url>" for p in ("/", "/pricing"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )
    return Response(body, mimetype="application/xml")


# Hosts that serve the chatbot0 pages at their root. Override with CHATBOT0_HOSTS (comma-separated).
DEFAULT_HOSTS = "chatbot0.smalltech.in"


def _chat_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get("CHATBOT0_HOSTS", DEFAULT_HOSTS).split(",") if h.strip()}


def register_host_routing(app: Flask) -> None:
    """Serve the chatbot0 pages at the root of the chatbot0 host(s).

    Only GET/HEAD on a handful of exact paths is intercepted, and only for those hosts; anything else falls
    through to the normal routes (API, health, Swagger)."""
    hosts = _chat_hosts()
    pages = {"/": landing, "/pricing": pricing, "/robots.txt": robots, "/sitemap.xml": sitemap}

    @app.before_request
    def _chatbot0_host_root():
        if request.method not in ("GET", "HEAD"):
            return None
        host = (request.host or "").split(":")[0].lower()
        if host not in hosts:
            return None
        path = request.path
        if path in pages:
            return pages[path]()
        if path.startswith("/static/"):
            return send_from_directory(_STATIC_DIR, path[len("/static/"):])
        return None
