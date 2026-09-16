
import hashlib
import json
import logging
import re
import time
import uuid
import httpx
import os
from dotenv import load_dotenv

import base64
import mimetypes
import threading
from collections import Counter
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr
from html import escape as html_escape
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from xml.sax.saxutils import escape as xml_escape

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
load_dotenv()

log = logging.getLogger("agent.tools")


def _env(*names, default=None):
    """The value of the first of these variables that is set."""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _env_int(name, default):
    """Environment values are always text, so numbers need int()."""
    try:
        return int(_env(name, default=str(default)))
    except ValueError:
        return default


def _env_bool(name, default):
    return _env(name, default="true" if default else "false").lower() in {"1", "true", "yes", "on"}


SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
BUSINESS_NAME = os.getenv("BUSINESS_NAME") or "Dubai Property Explorer"
GENERATED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) , 'generated')  # the PDF drafts
os.makedirs(GENERATED_DIR, exist_ok=True)
MAX_EMAILS_PER_SESSION  = _env_int("MAX_EMAILS_PER_SESSION", 3)
MAX_INQUIRIES_PER_SESSION  = _env_int("MAX_INQUIRIES_PER_SESSION", 3)
MAX_EMAILS_PER_DAY = _env_int("MAX_EMAILS_PER_DAY", 20)  # for the whole server


def _supabase_key_role(key):
    """'anon' for a public key; 'service_role' or 'secret' for keys that skip row level security."""
    if not key:
        return None
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "anon"
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except (IndexError, ValueError, AttributeError):
        return None


if _supabase_key_role(SUPABASE_ANON_KEY) in {"service_role", "secret"}:
    raise RuntimeError(
        "SUPABASE_ANON_KEY is a secret (service_role) key. Use the public anon key: "
        "the agent must stay inside the database's row level security."
    )


# ============================================================================
# Links: where this API lives, and where the website lives
# ============================================================================
# The API knows its own address without any setting: Render gives every web service
# RENDER_EXTERNAL_URL (e.g. https://my-agent.onrender.com). On your PC it is
# http://localhost:<PORT>. Set PUBLIC_API_URL only to override both.
API_BASE_URL = (
    _env("PUBLIC_API_URL", "RENDER_EXTERNAL_URL") or f"http://localhost:{_env('PORT', default='8000')}"
).rstrip("/")

# The website's address is NOT needed when you deploy the backend:
#   - listing cards carry a path ("/property/<id>"), and React turns it into a link
#     on whatever domain the website runs on (window.location.origin);
#   - for the PDF footer and the email, the website sends window.location.origin with
#     each chat message and main.py stores it as session.website_url.
# If you later set WEBSITE_URL on Render, it always wins over what a browser sends.
WEBSITE_URL = (_env("WEBSITE_URL") or "").rstrip("/")
ORIGIN_PATTERN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")  # scheme + host (+ port), nothing else


def website_url(session=None):
    """The website's origin, e.g. "https://my-site.onrender.com", or "" when it isn't known."""
    if WEBSITE_URL:
        return WEBSITE_URL
    candidate = str(getattr(session, "website_url", "") or "").strip().rstrip("/")
    return candidate if ORIGIN_PATTERN.fullmatch(candidate) else ""


def listing_link(session, listing_id):
    """A listing's page: the full URL when the website's origin is known, otherwise just the path."""
    return f"{website_url(session)}/property/{listing_id}"


READABLE_TABLES = frozenset({"listings", "media", "uae_areas"})
INSERTABLE_TABLES = frozenset({"inquiries"})


_http = httpx.Client(timeout=15.0)

# ============================================================================
# Setup supabase database
# ============================================================================

class ForbiddenTableError(PermissionError):
    """The agent tried to use a table it isn't allowed to touch."""

class DatabaseError(RuntimeError):
    """Supabase was unreachable or refused the request."""


def _headers(extra=None):
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers



def _check(response, action):
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("message") or response.text
    except ValueError:
        detail = response.text
    raise DatabaseError(f"Could not {action}: {detail} (HTTP {response.status_code})")



def select(table, params):
    """Read rows. `params` is a list of (name, value) pairs in Supabase REST syntax, for example [("select", "id,title"), ("price_aed", "lte.5000000")]."""
    if table not in READABLE_TABLES:
        raise ForbiddenTableError(f"The agent is not allowed to read the '{table}' table.")
    try:
        response = _http.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_headers())
    except httpx.HTTPError as err:
        raise DatabaseError(f"Could not reach Supabase: {err}") from err

    _check(response, f"read {table}")
    return response.json()


def insert(table, row):
    """Add one row. Nothing is read back: visitors may add enquiries but never see them."""
    if table not in INSERTABLE_TABLES:
        raise ForbiddenTableError(f"The agent is not allowed to write to the '{table}' table.")
    try:
        response = _http.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            json=row,
            headers=_headers({"Prefer": "return=minimal", "Content-Type": "application/json"}),
        )
    except httpx.HTTPError as err:
        raise DatabaseError(f"Could not reach Supabase: {err}") from err

    _check(response, f"save to {table}")




# ============================================================================
# Setup Email sender  (the same way as automated_Business_sales_report_generator/main.py)
# ============================================================================
# token.json is looked for in the folder you start the server from. If there is no token,
# get_credentials() opens the browser once to sign in and saves token.json there.
# On Render there is no browser: set GOOGLE_REFRESH_TOKEN, client_id and client_secret instead.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify", # "https://mail.google.com/"
]

TOKEN_PATH = os.path.join(os.path.abspath("./"), "token.json")
print(TOKEN_PATH)


class EmailError(RuntimeError):
    """The email could not be sent."""


def _from_env():
    rt = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not rt:
        return None

    return Credentials(
        token=None,
        refresh_token=rt,
        client_id=os.environ["client_id"],
        client_secret=os.environ["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )

def _from_file():
    if os.path.exists(TOKEN_PATH):
        return Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    return None




def get_credentials():
    creds = _from_env() or _from_file()

    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            creds.refresh(Request())
        else:
            config = {
                "installed": {
                    "client_id": os.environ["client_id"],
                    "client_secret": os.environ["client_secret"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(config, SCOPES)

            creds = flow.run_local_server(port=0, prompt="consent")
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
    return creds


def get_service():
    return build("gmail", "v1", credentials=get_credentials())




# Build gmail orch. class
class Gmail:
    def __init__(self, service=None):
        self.svc = get_service()
        self.me = "me"

    # ===================== send an email with attachments =====================
    def send_email(self, to, subject, body_text, body_html=None, attachments=None):
        # attachments = [(file_path, filename), ...]
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")

        for path, filename in attachments or []:
            ctype, encoding = mimetypes.guess_type(filename)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(path, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=filename)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self.svc.users().messages().send(userId=self.me, body={"raw": raw}).execute()


def email_configured():
    """On your PC get_credentials() can open the browser to sign in, so email always works there.
    A server (Render) has no browser to open, so it needs GOOGLE_REFRESH_TOKEN or token.json."""
    if os.getenv("RENDER"):
        return bool(os.getenv("GOOGLE_REFRESH_TOKEN")) or os.path.exists(TOKEN_PATH)
    return True


def email_method():
    if os.getenv("GOOGLE_REFRESH_TOKEN"):
        return "gmail (GOOGLE_REFRESH_TOKEN)"
    if os.path.exists(TOKEN_PATH):
        return f"gmail ({TOKEN_PATH})"
    if not os.getenv("RENDER"):
        return "gmail (the browser opens to sign in on the first email)"
    return None


def _one_line(text):
    return " ".join(str(text or "").split())  # no line breaks can sneak into a header


gm = None  # the Gmail client, like `gm = Gmail()` in the sales report bot; made on the first email so the server starts without signing in
_gmail_lock = threading.Lock()  # one email at a time: the Google client isn't thread-safe


def send_pdf_email(*, to_email, to_name, subject, text_body, html_body, pdf_file, filename):
    """Email one PDF from your Gmail with the Gmail class."""
    global gm
    if not email_configured():
        raise EmailError("Email isn't set up on the server: add GOOGLE_REFRESH_TOKEN, client_id and client_secret.")

    with _gmail_lock:
        try:
            if gm is None:
                gm = Gmail()
            gm.send_email(
                to=formataddr((_one_line(to_name), _one_line(to_email))),
                subject=_one_line(subject),
                body_text=text_body,
                body_html=html_body,
                attachments=[(pdf_file, filename)],
            )
        except RefreshError as err:
            gm = None
            raise EmailError("Google refused the saved Gmail token (it expired or was revoked). Delete token.json and sign in again.") from err
        except HttpError as err:
            gm = None
            raise EmailError(f"Gmail could not send the email (HTTP {err.resp.status}: {getattr(err, 'reason', '')}).") from err
        except KeyError as err:
            raise EmailError(f"{err.args[0]} is missing in .env.") from err
        except Exception as err:
            gm = None
            raise EmailError(f"Could not send the email: {err}") from err


class DailyEmailLimit:
    """A server-wide cap on emails per day, on top of the per-session cap."""

    def __init__(self, limit):
        self.limit = limit
        self._day = date.today()
        self._count = 0
        self._lock = threading.Lock()

    def _roll(self):
        if date.today() != self._day:
            self._day, self._count = date.today(), 0

    def available(self):
        with self._lock:
            self._roll()
            return self._count < self.limit

    def record(self):
        with self._lock:
            self._roll()
            self._count += 1


daily_emails = DailyEmailLimit(MAX_EMAILS_PER_DAY)


# ============================================================================
# Formatting: prices, listing cards and cost estimates
# (the same rules as the website's src/lib/format.js and PaymentCalculator.jsx)
# ============================================================================

def _round(value):
    """Round half up, like JavaScript's Math.round (Python's round() rounds half to even)."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def compact_aed(value):
    """2450000 -> "2.45M", 850000 -> "850K"."""
    if value is None:
        return "—"
    if value >= 1_000_000:
        millions = value / 1_000_000
        text = f"{millions:.1f}" if millions >= 10 else f"{millions:.2f}"
        if millions < 10 and text.endswith("0"):
            text = text[:-1]
        return f"{text}M"
    if value >= 1_000:
        return f"{_round(value / 1_000)}K"
    return str(value)


def full_aed(value):
    """2450000 -> "AED 2,450,000"."""
    if value is None:
        return "Price on application"
    return f"AED {_round(value):,}"


def price_suffix(listing_type):
    return "per year" if listing_type == "rent" else ""


def sqft_label(value):
    return f"{_round(value):,} sqft" if value else "—"


def price_per_sqft(price, size):
    return _round(price / size) if price and size else None


def monthly_payment(principal, annual_rate_pct, years):
    """Flat monthly payment for a repayment mortgage."""
    rate = annual_rate_pct / 100 / 12
    months = years * 12
    if not principal or months <= 0:
        return 0
    if rate == 0:
        return principal / months
    return principal * rate / (1 - (1 + rate) ** -months)


def property_card(listing, emirate=None):
    """Everything the chat needs to draw a listing card. Its keys are a superset of what
    src/components/explorer/ListingCard.jsx reads, so React can pass a card straight in.

    Only the PATH is sent ("/property/<id>"). React makes the link on its own domain,
    e.g. <Link to={card.path}> or new URL(card.path, window.location.origin)."""
    listing_id = listing["id"]
    listing_type = listing.get("listing_type")
    price = listing.get("price_aed")
    return {
        "id": listing_id,
        "title": listing.get("title"),
        "area": listing.get("area"),
        "emirate": emirate,
        "address": listing.get("address"),
        "listing_type": listing_type,
        "badge": "For rent" if listing_type == "rent" else "For sale",
        "property_type": listing.get("property_type"),
        "price_aed": price,
        "price_label": f"AED {compact_aed(price)}",
        "price_suffix": price_suffix(listing_type),
        "bedrooms": listing.get("bedrooms"),
        "bathrooms": listing.get("bathrooms"),
        "size_sqft": listing.get("size_sqft"),
        "size_label": sqft_label(listing.get("size_sqft")),
        "image_url": listing.get("image_url"),
        "lat": listing.get("lat"),
        "lng": listing.get("lng"),
        "path": f"/property/{listing_id}",
    }


# Government and agency fees per emirate — the figures PaymentCalculator.jsx uses.
COSTS = {
    "Dubai": {
        "transfer_label": "DLD transfer", "transfer_rate": 0.04,
        "admin_label": "Registration and trustee fees", "admin_fee": 5_250,
        "agency_rate": 0.02,
        "tenancy_label": "Ejari registration", "tenancy_fee": 220,
    },
    "Abu Dhabi": {
        "transfer_label": "DMT registration", "transfer_rate": 0.02,
        "admin_label": "Registration and admin", "admin_fee": 1_000,
        "agency_rate": 0.02,
        "tenancy_label": "Tawtheeq registration", "tenancy_fee": 1_000,
    },
}


def _within(value, low, high, default, label, unit, adjustments):
    if value is None:
        return default
    if value < low:
        adjustments.append(f"{label} raised to the minimum of {low:g}{unit}")
        return low
    if value > high:
        adjustments.append(f"{label} lowered to the maximum of {high:g}{unit}")
        return high
    return value


def estimate_costs(price, listing_type, emirate, down_payment_pct=None, years=None,
                   interest_rate_pct=None, cheques=None):
    """What buying or renting would cost, as labelled lines plus the key totals."""
    schedule = emirate if emirate in COSTS else "Dubai"
    fees = COSTS[schedule]
    adjustments = []

    if listing_type == "rent":
        count = int(cheques) if cheques else 4
        if count not in (1, 2, 4, 12):
            adjustments.append(f"{count} cheques isn't a usual option, so 4 were used")
            count = 4
        each = price / count
        due = each + price * 0.10 + fees["tenancy_fee"]
        return {
            "type": "rent",
            "fee_schedule": schedule,
            "lines": [
                {"label": "Annual rent", "value": full_aed(price)},
                {"label": f"Each of {count} cheque{'s' if count > 1 else ''}", "value": full_aed(each), "strong": True},
                {"label": "Security deposit (5%)", "value": full_aed(price * 0.05)},
                {"label": "Agency fee (5%)", "value": full_aed(price * 0.05)},
                {"label": fees["tenancy_label"], "value": full_aed(fees["tenancy_fee"])},
                {"label": "Due before keys", "value": full_aed(due), "strong": True},
            ],
            "due_before_keys_aed": _round(due),
            "note": "Indicative only. Chiller, DEWA deposit and any furnishing premium are agreed "
                    "with the landlord and are not included.",
            "adjusted": adjustments,
        }

    down = _within(down_payment_pct, 20, 80, 20, "Down payment", "%", adjustments)
    term = _within(years, 5, 25, 25, "Mortgage term", " years", adjustments)
    rate = _within(interest_rate_pct, 2.5, 8, 4.25, "Interest rate", "%", adjustments)
    deposit = price * down / 100
    loan = price - deposit
    monthly = monthly_payment(loan, rate, term)
    transfer = price * fees["transfer_rate"]
    agency = price * fees["agency_rate"]
    upfront = deposit + transfer + agency + fees["admin_fee"]
    return {
        "type": "sale",
        "fee_schedule": schedule,
        "lines": [
            {"label": f"Monthly repayment ({term:g} years at {rate:g}%)", "value": full_aed(monthly), "strong": True},
            {"label": f"Down payment ({down:g}%)", "value": full_aed(deposit)},
            {"label": "Loan amount", "value": full_aed(loan)},
            {"label": f"{fees['transfer_label']} ({fees['transfer_rate'] * 100:g}%)", "value": full_aed(transfer)},
            {"label": f"Agency fee ({fees['agency_rate'] * 100:g}%)", "value": full_aed(agency)},
            {"label": fees["admin_label"], "value": full_aed(fees["admin_fee"])},
            {"label": "Cash needed up front", "value": full_aed(upfront), "strong": True},
        ],
        "monthly_repayment_aed": _round(monthly),
        "cash_up_front_aed": _round(upfront),
        "note": f"Indicative only and not a mortgage offer. Government fees are the {schedule} schedule. "
                "Non-residents are usually capped at a 50% loan-to-value; bank arrangement and "
                "valuation fees are excluded.",
        "adjusted": adjustments,
    }


# ============================================================================
# PDF builder: turns a draft into a branded PDF (reportlab)
# The model writes the prose sections; every price, fact, photo and cost table
# comes from the database, so the document can't contain made-up numbers.
# ============================================================================

INK = colors.HexColor("#151C17")
MUTE = colors.HexColor("#3A473D")
BRASS = colors.HexColor("#B08D57")
SAND = colors.HexColor("#E5EAE6")
SAGE = colors.HexColor("#D2DAD5")

KIND_LABELS = {
    "property_brochure": "Property brochure",
    "shortlist": "Shortlist",
    "comparison": "Comparison",
    "cost_estimate": "Cost estimate",
    "viewing_request": "Viewing request",
    "requirements_summary": "Your requirements",
}


def _register_fonts():
    """Arial and Georgia on Windows, DejaVu or Liberation on Linux (Render), when present
    (they cover accents and symbols); otherwise reportlab's built-in Helvetica and Times."""
    candidates = {
        "AgentBody": ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/Library/Fonts/Arial.ttf"],
        "AgentBold": ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"],
        "AgentDisplay": ["C:/Windows/Fonts/georgia.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                         "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf", "/Library/Fonts/Georgia.ttf"],
    }
    found = {}
    for name, paths in candidates.items():
        for path in paths:
            if os.path.exists(path):
                try:
                    pdfmetrics.registerFont(TTFont(name, path))
                    found[name] = name
                    break
                except Exception:  # an unreadable font file: try the next one
                    continue
    return found.get("AgentBody", "Helvetica"), found.get("AgentBold", "Helvetica-Bold"), found.get("AgentDisplay", "Times-Roman")


BODY, BOLD, DISPLAY = _register_fonts()

STYLES = {
    "eyebrow": ParagraphStyle("eyebrow", fontName=BOLD, fontSize=8, leading=11, textColor=BRASS, spaceAfter=4),
    "title": ParagraphStyle("title", fontName=DISPLAY, fontSize=26, leading=31, textColor=INK, spaceAfter=6),
    "subtitle": ParagraphStyle("subtitle", fontName=BODY, fontSize=9.5, leading=14, textColor=MUTE),
    "h2": ParagraphStyle("h2", fontName=DISPLAY, fontSize=16, leading=20, textColor=INK, spaceBefore=10, spaceAfter=6),
    "h3": ParagraphStyle("h3", fontName=DISPLAY, fontSize=13.5, leading=17, textColor=INK, spaceAfter=2),
    "body": ParagraphStyle("body", fontName=BODY, fontSize=10, leading=15, textColor=MUTE, spaceAfter=6),
    "small": ParagraphStyle("small", fontName=BODY, fontSize=8, leading=11.5, textColor=MUTE, spaceAfter=2),
    "cell": ParagraphStyle("cell", fontName=BODY, fontSize=9, leading=12, textColor=INK),
    "cell_strong": ParagraphStyle("cell_strong", fontName=BOLD, fontSize=9, leading=12, textColor=INK),
    "label": ParagraphStyle("label", fontName=BOLD, fontSize=7.5, leading=11, textColor=MUTE),
}


def _p(text, style):
    return Paragraph(xml_escape(str(text)).replace("\n", "<br/>"), STYLES[style])


def _paragraphs(text):
    return [chunk.strip() for chunk in str(text or "").split("\n\n") if chunk.strip()]


_photo_cache = {}


def _fetchable_url(url):
    """Unsplash sends AVIF/WebP to browsers; ask for a JPEG instead."""
    parts = urlparse(url)
    if parts.netloc == "images.unsplash.com":
        query = dict(parse_qsl(parts.query))
        query.pop("auto", None)
        query.update({"fm": "jpg", "q": "75", "w": "1400"})
        return urlunparse(parts._replace(query=urlencode(query)))
    return url


def _photo(url, width, height):
    """The photo cropped to fill width x height, or None if it can't be loaded."""
    if not url:
        return None
    try:
        data = _photo_cache.get(url)
        if data is None:
            response = httpx.get(_fetchable_url(url), timeout=20, follow_redirects=True)
            response.raise_for_status()
            data = response.content
            if len(_photo_cache) > 60:
                _photo_cache.clear()
            _photo_cache[url] = data
        picture = PILImage.open(BytesIO(data)).convert("RGB")
        w, h = picture.size
        target = width / height
        if w / h > target:  # too wide: trim the sides
            new_w = int(h * target)
            picture = picture.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
        else:  # too tall: trim top and bottom
            new_h = int(w / target)
            picture = picture.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
        picture.thumbnail((1200, 1200))  # sharp in print, small enough to email
        buffer = BytesIO()
        picture.save(buffer, "JPEG", quality=78, optimize=True)
        buffer.seek(0)
        return Image(buffer, width=width, height=height)
    except Exception:
        return None


def _page_decoration(business_name, site_url):
    def draw(canvas, doc):
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.rect(0, height - 13 * mm, width, 13 * mm, stroke=0, fill=1)
        canvas.setFont(BOLD, 7.5)
        canvas.setFillColor(SAND)
        canvas.drawString(18 * mm, height - 8.3 * mm, business_name.upper())
        canvas.setFillColor(BRASS)
        canvas.drawRightString(width - 18 * mm, height - 8.3 * mm, date.today().strftime("%d %B %Y"))
        canvas.setStrokeColor(SAGE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 14 * mm, width - 18 * mm, 14 * mm)
        canvas.setFont(BODY, 7.5)
        canvas.setFillColor(MUTE)
        if site_url:
            canvas.drawString(18 * mm, 9 * mm, site_url)
        canvas.drawRightString(width - 18 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def _is_rent(listing):
    return listing["listing_type"] == "rent"


def _price(listing):
    return f"{full_aed(listing['price_aed'])}{' per year' if _is_rent(listing) else ''}"


def _bedrooms(listing):
    return "Studio" if listing.get("bedrooms") == 0 else str(listing.get("bedrooms"))


def _per_sqft(listing):
    value = price_per_sqft(listing["price_aed"], listing.get("size_sqft"))
    if not value:
        return "—"
    return f"AED {value:,}{' / year' if _is_rent(listing) else ''}"


def _comparison_table(listings, width):
    label_width = 28 * mm
    column = (width - label_width) / len(listings)
    rows = [
        ("Price", [f"AED {compact_aed(l['price_aed'])}{' / year' if _is_rent(l) else ''}" for l in listings]),
        ("Where", [f"{l['area']}, {l.get('emirate') or ''}".strip(", ") for l in listings]),
        ("Type", [str(l["property_type"]).title() for l in listings]),
        ("Bedrooms", [_bedrooms(l) for l in listings]),
        ("Bathrooms", [str(l.get("bathrooms")) for l in listings]),
        ("Size", [sqft_label(l.get("size_sqft")) for l in listings]),
        ("Per sqft", [_per_sqft(l) for l in listings]),
    ]
    data = [[_p("", "label"), *[_p(l["title"], "cell_strong") for l in listings]]]
    data += [[_p(label.upper(), "label"), *[_p(value, "cell") for value in values]] for label, values in rows]
    table = Table(data, colWidths=[label_width] + [column] * len(listings), repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), SAND),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, SAGE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _cost_table(cost, width):
    """The cost lines and their note, kept on one page."""
    rows = [[_p(line["label"], "cell_strong" if line.get("strong") else "cell"),
             _p(line["value"], "cell_strong" if line.get("strong") else "cell")] for line in cost["lines"]]
    heading = "Buying costs" if cost["type"] == "sale" else "Renting costs"
    table = Table([[_p(f"{heading.upper()} · {cost['fee_schedule'].upper()} FEES", "label"), _p("", "label")]] + rows,
                  colWidths=[width * 0.62, width * 0.38])
    table.setStyle(TableStyle([
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), SAND),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, SAGE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return KeepTogether([table, Spacer(1, 1.5 * mm), _p(cost["note"], "small")])


def _property_block(listing, width, cost, detailed, site_url, lead=()):
    """One listing: title, photo beside its facts, then description, amenities and costs.
    `lead` goes in front of the title and stays on the same page as it."""
    photo_width, gap = 72 * mm, 6 * mm
    facts_width = width - photo_width - gap
    facts = [
        ("Price", _price(listing)),
        ("For", "Rent" if _is_rent(listing) else "Sale"),
        ("Type", str(listing["property_type"]).title()),
        ("Bedrooms", _bedrooms(listing)),
        ("Bathrooms", str(listing.get("bathrooms"))),
        ("Size", sqft_label(listing.get("size_sqft"))),
        ("Location", ", ".join(dict.fromkeys(filter(None, [listing.get("address"), listing["area"], listing.get("emirate")])))),
    ]
    fact_table = Table([[_p(k.upper(), "label"), _p(v, "cell")] for k, v in facts], colWidths=[24 * mm, facts_width - 24 * mm])
    fact_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, SAGE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    photo = _photo(listing.get("image_url"), photo_width, 52 * mm) or _p("", "cell")
    top = Table([[photo, fact_table]], colWidths=[photo_width + gap, facts_width])
    top.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), gap),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))

    heading = [*lead, _p(listing["title"], "h3"), Spacer(1, 2 * mm), top, Spacer(1, 3 * mm)]
    rest = []
    if detailed:
        gallery = [url for url in (listing.get("image_urls") or []) if url != listing.get("image_url")][:3]
        thumbs = [t for t in (_photo(url, (width - 8 * mm) / 3, 30 * mm) for url in gallery) if t]
        if thumbs:
            strip = Table([thumbs], colWidths=[width / 3] * len(thumbs))
            strip.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
            rest += [strip, Spacer(1, 3 * mm)]
        rest += [_p(paragraph, "body") for paragraph in _paragraphs(listing.get("description"))]
    amenities = listing.get("amenities") or []
    if amenities:
        rest.append(_p("Amenities: " + " · ".join(amenities[: 12 if detailed else 5]), "small"))
    if cost:
        rest += [Spacer(1, 2 * mm), _cost_table(cost, width)]
    if site_url:  # only when the website's address is known
        rest.append(_p(f"See it online: {site_url}/property/{listing['id']}", "small"))
    rest.append(Spacer(1, 7 * mm))
    return [KeepTogether(heading), *rest]


def build_draft_pdf(path, *, draft, listings, costs, cover_image_url, business_name, site_url=""):
    """Write the draft's PDF to `path`. `listings` carry an extra "emirate" key.
    `site_url` is the website's origin, or "" when it isn't known (links are then left out)."""
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=22 * mm, bottomMargin=20 * mm,
        title=draft["title"], author=business_name,
    )
    width = A4[0] - 36 * mm
    kind = draft["kind"]
    story = [
        _p(KIND_LABELS.get(kind, "Document").upper(), "eyebrow"),
        _p(draft["title"], "title"),
    ]
    subtitle = [f"Prepared for {draft['recipient_name']}" if draft.get("recipient_name") else None,
                date.today().strftime("%d %B %Y")]
    if listings:
        subtitle.append(f"{len(listings)} propert{'y' if len(listings) == 1 else 'ies'}")
    story += [_p(" · ".join(filter(None, subtitle)), "subtitle"), Spacer(1, 6 * mm)]

    cover = _photo(cover_image_url, width, 64 * mm)
    if cover:
        story += [cover, Spacer(1, 6 * mm)]

    for section in draft.get("sections") or []:
        paragraphs = [_p(paragraph, "body") for paragraph in _paragraphs(section.get("body"))]
        if section.get("heading"):  # a heading never ends a page on its own
            story.append(KeepTogether([_p(section["heading"], "h2"), *paragraphs[:1]]))
            story += paragraphs[1:]
        else:
            story += paragraphs

    if len(listings) >= 2 and kind in ("comparison", "shortlist"):
        story += [KeepTogether([_p("At a glance", "h2"), _comparison_table(listings, width)]), Spacer(1, 4 * mm)]

    detailed = kind == "property_brochure" or len(listings) == 1
    for number, listing in enumerate(listings):
        lead = [_p("The property" if len(listings) == 1 else "The properties", "h2")] if number == 0 else []
        story += _property_block(listing, width, costs.get(listing["id"]), detailed, site_url, lead=lead)

    source = site_url or "our website"
    story += [
        Spacer(1, 4 * mm),
        _p(f"Prepared by {business_name} from the listings on {source}. Prices, availability and "
           "fees can change, so please confirm the details with us before making a decision.", "small"),
    ]
    decoration = _page_decoration(business_name, site_url)
    doc.build(story, onFirstPage=decoration, onLaterPages=decoration)


# ============================================================================
# Tools: constants and argument helpers
# ============================================================================

LISTING_TYPES = ("sale", "rent")
# Must match the property_type check on the inquiries table.
PROPERTY_TYPES = ("apartment", "villa", "townhouse", "penthouse", "studio", "office")
EMIRATE_CENTERS = {"Dubai": [25.152, 55.24], "Abu Dhabi": [24.4539, 54.3773]}
LISTING_COLUMNS = (
    "id,title,area,address,price_aed,listing_type,property_type,bedrooms,bathrooms,size_sqft,"
    "description,image_url,image_urls,amenities,lat,lng,is_demo,sort_order,created_at"
)
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
DRAFT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s<>,;\"'()]+@[^@\s<>,;\"'()]+\.[A-Za-z]{2,}$")

_TYPE_WORDS = {
    "flat": "apartment", "flats": "apartment", "apartments": "apartment",
    "villas": "villa", "house": "villa", "houses": "villa",
    "townhouses": "townhouse", "town house": "townhouse",
    "penthouses": "penthouse", "studios": "studio", "offices": "office",
}
_INTENT_WORDS = {
    "buy": "sale", "buying": "sale", "purchase": "sale", "for sale": "sale",
    "rental": "rent", "renting": "rent", "lease": "rent", "to rent": "rent", "for rent": "rent",
}

# kind -> (what it is, fewest listings, most listings)
DRAFT_KINDS = {
    "property_brochure": ("One property in detail: photos, facts, description and amenities.", 1, 1),
    "shortlist": ("Several properties that match what the visitor wants.", 1, 6),
    "comparison": ("Two to four properties side by side.", 2, 4),
    "cost_estimate": ("What buying or renting would cost: fees, deposit, monthly payments.", 1, 3),
    "viewing_request": ("A printable letter listing properties the visitor wants to view — only when they ask for a document; to actually book a viewing use save_inquiry.", 1, 6),
    "requirements_summary": ("A summary of what the visitor is looking for and the next steps.", 0, 6),
}


class ToolError(ValueError):
    """Bad or missing arguments — the model should fix them or ask the visitor."""


def _clean(value, max_len):
    if value is None:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value)).strip()[:max_len]


def _text(args, key, max_len=200):
    return _clean(args.get(key), max_len)


def _to_number(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "").replace("aed", "").strip()
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(k|m|mn|million)?", text)
    if not match:
        raise ValueError
    return float(match.group(1)) * {"k": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6}.get(match.group(2) or "", 1)


def _number(args, key, low=0.0, high=None):
    value = args.get(key)
    if value is None or value == "":
        return None
    try:
        number = _to_number(value)
    except ValueError:
        raise ToolError(f"'{key}' must be a number (got {value!r}).") from None
    if number < low or (high is not None and number > high):
        raise ToolError(f"'{key}' must be between {low:g} and {high:g}." if high is not None else f"'{key}' must be at least {low:g}.")
    return number


def _listing_type(value, required=False):
    text = _clean(value, 20).lower()
    if not text:
        if required:
            raise ToolError("'listing_type' is required: \"sale\" or \"rent\".")
        return None
    text = _INTENT_WORDS.get(text, text)
    if text not in LISTING_TYPES:
        raise ToolError("'listing_type' must be \"sale\" or \"rent\".")
    return text


def _property_type(value):
    text = _clean(value, 30).lower()
    if not text:
        return None
    text = _TYPE_WORDS.get(text, text)
    if text not in PROPERTY_TYPES:
        raise ToolError(f"'property_type' must be one of {list(PROPERTY_TYPES)}.")
    return text


def _ids(value, fewest, most):
    if isinstance(value, str):
        value = [value]
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ToolError("'property_ids' must be a list of listing ids.")
    ids = list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))
    bad = [i for i in ids if not ID_PATTERN.fullmatch(i)]
    if bad:
        raise ToolError(f"These aren't listing ids: {bad}. Use ids from search results.")
    if not fewest <= len(ids) <= most:
        raise ToolError(f"Give between {fewest} and {most} property_ids.")
    return ids


def _is_yes(value):
    return value is True or str(value).strip().lower() in {"true", "yes", "1"}


def _quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _in(values):
    return "in.(" + ",".join(_quote(v) for v in values) + ")"


# ============================================================================
# Tools: reference data (areas and photos change rarely, so keep them a few minutes)
# ============================================================================

_cache = {}


def _cached(key, seconds, load):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = load()
    _cache[key] = (time.time(), value)
    return value


def all_areas():
    return _cached("areas", 300, lambda: select(
        "uae_areas", [("select", "name,emirate,aliases,lat,lng"), ("order", "sort_order.asc")]
    ))


def emirate_of(area_name):
    return next((a["emirate"] for a in all_areas() if a["name"] == area_name), None)


def photo_url(key):
    photos = _cached("photos", 600, lambda: select("media", [("select", "key,url"), ("kind", "eq.photo")]))
    return next((p["url"] for p in photos if p["key"] == key), None)


def _normalise(text):
    return re.sub(r"\s+", " ", re.sub(r"[-_,./]", " ", str(text or "").lower())).strip()


def _area_place(area):
    return {"name": area["name"], "emirate": area["emirate"], "isEmirate": False,
            "center": [area["lat"], area["lng"]], "area_names": [area["name"]]}


def _emirate_place(emirate, areas):
    return {"name": emirate, "emirate": emirate, "isEmirate": True, "center": EMIRATE_CENTERS.get(emirate),
            "area_names": [a["name"] for a in areas if a["emirate"] == emirate]}


def resolve_place(text):
    """'marina' -> Dubai Marina; 'abu dhabi' -> the whole emirate; None when unknown.
    The longest matching area name or alias wins, like the website's chat."""
    wanted = _normalise(text)
    if not wanted:
        return None
    areas = all_areas()
    emirates = sorted({a["emirate"] for a in areas}, key=len, reverse=True)
    for emirate in emirates:
        if wanted == _normalise(emirate):
            return _emirate_place(emirate, areas)
    best = None
    for area in areas:
        for key in [area["name"], *(area.get("aliases") or [])]:
            candidate = _normalise(key)
            if candidate and re.search(rf"\b{re.escape(candidate)}\b", wanted) and (best is None or len(candidate) > best[0]):
                best = (len(candidate), area)
    if best is None and len(wanted) >= 4:  # part of a name, e.g. "saadiyat"
        best = next(((0, a) for a in areas if wanted in _normalise(a["name"])), None)
    if best:
        return _area_place(best[1])
    for emirate in emirates:
        if re.search(rf"\b{re.escape(_normalise(emirate))}\b", wanted):
            return _emirate_place(emirate, areas)
    return None


def map_focus(place):
    """The shape the website's map already uses: {name, emirate, isEmirate, center: [lat, lng]}."""
    return {key: place[key] for key in ("name", "emirate", "isEmirate", "center")} if place else None


def focus_for(text):
    return map_focus(resolve_place(text)) if text else None


# ============================================================================
# Tools: listings
# ============================================================================

def _listings(params):
    return select("listings", [("select", LISTING_COLUMNS), *params])


def _listings_by_ids(ids):
    return _listings([("id", _in(ids))]) if ids else []


def _require_listing(property_id):
    listing_id = _clean(property_id, 80)
    if not listing_id or not ID_PATTERN.fullmatch(listing_id):
        raise ToolError("'property_id' is required. Use an id from search_properties.")
    rows = _listings_by_ids([listing_id])
    if not rows:
        raise ToolError(f"There's no listing with id {listing_id!r}. Search again for current ids.")
    return rows[0]


def _summary(listing):
    rent = listing["listing_type"] == "rent"
    return {
        "id": listing["id"],
        "title": listing["title"],
        "area": listing["area"],
        "emirate": emirate_of(listing["area"]),
        "listing_type": listing["listing_type"],
        "property_type": listing["property_type"],
        "price": f"AED {compact_aed(listing['price_aed'])}{' per year' if rent else ''}",
        "price_aed": listing["price_aed"],
        "bedrooms": listing["bedrooms"],
        "bathrooms": listing["bathrooms"],
        "size_sqft": listing.get("size_sqft"),
        "aed_per_sqft": price_per_sqft(listing["price_aed"], listing.get("size_sqft")),
        "top_amenities": (listing.get("amenities") or [])[:3],
    }


def cards_for(property_ids):
    """Listing cards for the chat, in the order given. Unknown ids are dropped, so the
    model can never put a card on screen for a listing that doesn't exist."""
    ids = [i for i in dict.fromkeys(property_ids or []) if isinstance(i, str) and ID_PATTERN.fullmatch(i)][:6]
    rows = {r["id"]: r for r in _listings_by_ids(ids)}
    return [property_card(rows[i], emirate_of(rows[i]["area"])) for i in ids if i in rows]


def _unknown_place(text):
    return {"error": "unknown_area", "message": f"'{text}' isn't one of the areas this website covers.",
            "areas_we_cover": [a["name"] for a in all_areas()]}


# ============================================================================
# Tools: finding properties
# ============================================================================

def search_properties(args, session):
    params, filters, place = [], {}, None

    where = _text(args, "area", 80) or _text(args, "emirate", 40)
    if where:
        place = resolve_place(where)
        if not place:
            return _unknown_place(where)
        params.append(("area", _in(place["area_names"])))
        filters["emirate" if place["isEmirate"] else "area"] = place["name"]
        session.events["map_focus"] = map_focus(place)

    listing_type = _listing_type(args.get("listing_type"))
    if listing_type:
        params.append(("listing_type", f"eq.{listing_type}"))
        filters["listing_type"] = listing_type
    property_type = _property_type(args.get("property_type"))
    if property_type:
        params.append(("property_type", f"eq.{property_type}"))
        filters["property_type"] = property_type

    ranges = [
        ("min_price", "price_aed", "gte"), ("max_price", "price_aed", "lte"),
        ("min_bedrooms", "bedrooms", "gte"), ("max_bedrooms", "bedrooms", "lte"),
        ("min_bathrooms", "bathrooms", "gte"),
        ("min_size_sqft", "size_sqft", "gte"), ("max_size_sqft", "size_sqft", "lte"),
    ]
    for arg, column, operator in ranges:
        value = _number(args, arg, 0, 10_000_000_000)
        if value is not None:
            params.append((column, f"{operator}.{int(value)}"))
            filters[arg] = int(value)

    order = {
        "price_asc": "price_aed.asc", "price_desc": "price_aed.desc",
        "size_desc": "size_sqft.desc.nullslast", "newest": "created_at.desc",
    }.get(_text(args, "sort", 20), "sort_order.asc")
    params.append(("order", order))

    rows = _listings(params)

    keywords = [w for w in _normalise(_text(args, "keywords", 120)).split() if len(w) > 2]
    if keywords:
        def matches(row):
            haystack = _normalise(" ".join([row["title"], row.get("description") or "", row.get("address") or "",
                                            " ".join(row.get("amenities") or [])]))
            return all(word in haystack for word in keywords)
        rows = [row for row in rows if matches(row)]
        filters["keywords"] = " ".join(keywords)

    limit = int(_number(args, "limit", 1, 10) or 6)
    result = {"total_matches": len(rows), "showing": min(limit, len(rows)), "filters": filters,
              "results": [_summary(r) for r in rows[:limit]]}
    if not rows:
        result["hint"] = "Nothing matches every filter. Offer to widen the budget, bedrooms or area."
    return result


def get_property_details(args, session):
    listing = _require_listing(args.get("property_id"))
    session.events["map_focus"] = focus_for(listing["area"])
    return {"property": {
        **_summary(listing),
        "address": listing.get("address"),
        "description": listing.get("description"),
        "amenities": listing.get("amenities") or [],
        "photos": len(listing.get("image_urls") or []),
        "is_sample_listing": bool(listing.get("is_demo")),
        "page": listing_link(session, listing["id"]),
    }}


def find_similar_properties(args, session):
    listing = _require_listing(args.get("property_id"))
    limit = int(_number(args, "limit", 1, 6) or 3)
    emirate = emirate_of(listing["area"])
    pool = [r for r in _listings([("listing_type", f"eq.{listing['listing_type']}")]) if r["id"] != listing["id"]]
    by_price = lambda row: abs(row["price_aed"] - listing["price_aed"])  # noqa: E731
    same_area = sorted((r for r in pool if r["area"] == listing["area"]), key=by_price)
    same_emirate = sorted((r for r in pool if r["area"] != listing["area"] and emirate_of(r["area"]) == emirate), key=by_price)
    elsewhere = sorted((r for r in pool if emirate_of(r["area"]) != emirate), key=by_price)
    picks = (same_area + same_emirate + elsewhere)[:limit]
    return {"similar_to": listing["id"], "results": [_summary(r) for r in picks]}


def compare_properties(args, session):
    ids = _ids(args.get("property_ids"), 2, 4)
    rows = {r["id"]: r for r in _listings_by_ids(ids)}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise ToolError(f"No listing with id(s) {missing}.")
    items = [_summary(rows[i]) for i in ids]
    highlights = {}
    for listing_type in LISTING_TYPES:
        group = [i for i in items if i["listing_type"] == listing_type]
        if len(group) >= 2:
            with_size = [i for i in group if i["aed_per_sqft"]]
            highlights[listing_type] = {
                "lowest_price": min(group, key=lambda i: i["price_aed"])["id"],
                "largest": max(group, key=lambda i: i["size_sqft"] or 0)["id"],
                "lowest_aed_per_sqft": min(with_size, key=lambda i: i["aed_per_sqft"])["id"] if with_size else None,
            }
    mixed = len({i["listing_type"] for i in items}) > 1
    return {"properties": items, "highlights": highlights,
            "note": "These mix sale and rental listings; compare prices within each group only." if mixed else None}


# ============================================================================
# Tools: areas and money
# ============================================================================

def list_areas(args, session):
    emirate = None
    if _text(args, "emirate", 40):
        place = resolve_place(args["emirate"])
        if not place or not place["isEmirate"]:
            raise ToolError("'emirate' must be \"Dubai\" or \"Abu Dhabi\".")
        emirate = place["name"]
    counts = Counter(row["area"] for row in select("listings", [("select", "area")]))
    areas = [{"name": a["name"], "emirate": a["emirate"], "listings": counts.get(a["name"], 0)}
             for a in all_areas() if not emirate or a["emirate"] == emirate]
    return {"areas": areas, "areas_with_listings": sum(1 for a in areas if a["listings"])}


def area_market_summary(args, session):
    where = _text(args, "area", 80) or _text(args, "emirate", 40)
    if not where:
        raise ToolError("Give an 'area' or an 'emirate'.")
    place = resolve_place(where)
    if not place:
        return _unknown_place(where)
    session.events["map_focus"] = map_focus(place)
    rows = _listings([("area", _in(place["area_names"])), ("order", "price_aed.asc")])

    def stats(group):
        if not group:
            return None
        prices = [r["price_aed"] for r in group]
        per_sqft = [p for p in (price_per_sqft(r["price_aed"], r.get("size_sqft")) for r in group) if p]
        return {
            "listings": len(group),
            "lowest": full_aed(min(prices)),
            "average": full_aed(sum(prices) / len(prices)),
            "highest": full_aed(max(prices)),
            "average_aed_per_sqft": round(sum(per_sqft) / len(per_sqft)) if per_sqft else None,
            "property_ids": [r["id"] for r in group],
        }

    return {
        "place": place["name"],
        "emirate": place["emirate"],
        "for_sale": stats([r for r in rows if r["listing_type"] == "sale"]),
        "for_rent": stats([r for r in rows if r["listing_type"] == "rent"]),
        "property_types": dict(Counter(r["property_type"] for r in rows)),
        "note": "Based only on the listings on this website, not the whole market.",
    }


def estimate_costs_tool(args, session):
    if args.get("property_id"):
        listing = _require_listing(args["property_id"])
        price, listing_type, emirate = listing["price_aed"], listing["listing_type"], emirate_of(listing["area"])
        about = {"property_id": listing["id"], "title": listing["title"]}
    else:
        price = _number(args, "price_aed", 1, 10_000_000_000)
        if price is None:
            raise ToolError("Give a 'property_id', or a 'price_aed' together with 'listing_type'.")
        listing_type = _listing_type(args.get("listing_type"), required=True)
        place = resolve_place(_text(args, "area", 80) or _text(args, "emirate", 40))
        emirate = place["emirate"] if place else "Dubai"
        about = {"price_aed": price}
    estimate = estimate_costs(
        price, listing_type, emirate,
        down_payment_pct=_number(args, "down_payment_pct", 0, 100),
        years=_number(args, "years", 1, 40),
        interest_rate_pct=_number(args, "interest_rate_pct", 0, 25),
        cheques=_number(args, "cheques", 1, 12),
    )
    return {**about, **estimate}


# ============================================================================
# Tools: asking before acting
# ============================================================================

def _needs_confirmation(session, action, fingerprint, confirmed, summary):
    """None when the action may go ahead; otherwise tells the model to ask first.

    `confirmed` only counts if these exact details were already shown to the
    visitor in an EARLIER message. So one message can never both propose and
    carry out an enquiry or an email — the visitor always gets to say yes first.
    """
    pending = session.pending.get(action)
    if _is_yes(confirmed) and pending and pending["fingerprint"] == fingerprint and pending["turn"] < session.turn:
        session.pending.pop(action, None)
        session.events.pop("pending_confirmation", None)
        return None
    session.pending[action] = {"fingerprint": fingerprint, "turn": session.turn}
    session.events["pending_confirmation"] = {"action": action, "details": summary}
    return {
        "status": "needs_confirmation",
        "details": summary,
        "message": "Show these details to the visitor and ask them to confirm. Only after they agree in "
                   "their next message, call this tool again with the same details and \"confirmed\": true.",
    }


# ============================================================================
# Tools: enquiries
# ============================================================================

INQUIRY_FIELDS = [
    ("full_name", "full name"), ("email", "email address"), ("phone", "phone number"),
    ("area", "preferred area"), ("listing_type", "whether they want to buy or rent"),
    ("property_type", "property type"), ("budget_aed", "budget in AED"),
]


def save_inquiry(args, session):
    listing = _require_listing(args["property_id"]) if args.get("property_id") else None
    full_name = _text(args, "full_name", 120)
    email = _text(args, "email", 254)
    phone = _text(args, "phone", 40)
    area_text = _text(args, "area", 80) or (listing["area"] if listing else "")
    listing_type = _listing_type(args.get("listing_type")) or (listing["listing_type"] if listing else None)
    property_type = _property_type(args.get("property_type")) or (listing["property_type"] if listing else None)
    budget = _number(args, "budget_aed", 0, 10_000_000_000)
    if budget is None and listing:
        budget = listing["price_aed"]
    bedrooms = _number(args, "bedrooms", 0, 50)
    if bedrooms is None and listing:
        bedrooms = listing["bedrooms"]
    message = _text(args, "message", 1500)

    values = {"full_name": full_name, "email": email, "phone": phone, "area": area_text,
              "listing_type": listing_type, "property_type": property_type, "budget_aed": budget}
    missing = [label for key, label in INQUIRY_FIELDS if values[key] in (None, "")]
    if missing:
        return {"status": "missing_information", "missing": missing,
                "message": "Ask the visitor for all the missing details in one message, then call save_inquiry again."}
    if not EMAIL_PATTERN.fullmatch(email):
        raise ToolError(f"{email!r} doesn't look like an email address. Ask the visitor to check it.")
    if not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise ToolError(f"{phone!r} doesn't look like a phone number. Ask for it with the country code, e.g. +971 50 123 4567.")

    place = resolve_place(area_text)
    area = place["name"] if place and not place["isEmirate"] else area_text
    notes = [message] if message else []
    if listing:
        notes.append(f"Listing: {listing['title']} (/property/{listing['id']})")
    notes.append("Sent through the website chat assistant.")
    row = {
        "full_name": full_name, "email": email, "phone": phone, "area": area,
        "message": "\n\n".join(notes), "listing_type": listing_type, "property_type": property_type,
        "bedrooms": int(bedrooms) if bedrooms is not None else None, "budget_aed": int(round(budget)),
    }
    summary = {
        "name": full_name, "email": email, "phone": phone, "area": area,
        "looking_to": "buy" if listing_type == "sale" else "rent", "property_type": property_type,
        "bedrooms": row["bedrooms"], "budget": full_aed(row["budget_aed"]),
        "about_listing": listing["title"] if listing else None, "message": message or None,
    }
    # The message text can be reworded between the two calls, so it isn't part of the fingerprint.
    fingerprint = hashlib.sha256(json.dumps({k: v for k, v in row.items() if k != "message"}, sort_keys=True).encode()).hexdigest()

    if fingerprint in session.saved_inquiries:
        return {"status": "already_saved", "message": "This enquiry is already saved. Don't save it again."}
    if session.inquiries_saved >= MAX_INQUIRIES_PER_SESSION:
        return {"status": "limit_reached", "message": "Enough enquiries were saved in this chat. Tell the visitor an agent will be in touch."}
    waiting = _needs_confirmation(session, "save_inquiry", fingerprint, args.get("confirmed"), summary)
    if waiting:
        return waiting

    insert("inquiries", row)
    session.inquiries_saved += 1
    session.saved_inquiries.add(fingerprint)
    session.events["inquiry"] = {"status": "saved", **summary}
    return {"status": "saved", "message": "The enquiry is saved. Thank the visitor; an agent will contact them soon."}


# ============================================================================
# Tools: PDF drafts and email
# ============================================================================

COVER_PHOTO_KEYS = {"Dubai": "dubaiNight", "Abu Dhabi": "sheikhZayedMosque"}


def draft_pdf_path(draft_id):
    """The PDF file on disk for a draft id, or None. main.py's GET /drafts/<id>.pdf uses this.
    (Render's disk is wiped on restart, but so are the in-memory sessions that list the drafts.)"""
    if not DRAFT_ID_PATTERN.fullmatch(str(draft_id or "")):
        return None
    path = os.path.join(GENERATED_DIR, f"{draft_id}.pdf")
    return path if os.path.exists(path) else None


def _sections(value):
    if value in (None, ""):
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise ToolError("'sections' must be a list of {\"heading\": ..., \"body\": ...} objects.")
    sections = []
    for item in value[:8]:
        if isinstance(item, str):
            item = {"heading": "", "body": item}
        if isinstance(item, dict):
            heading, body = _clean(item.get("heading"), 120), _clean(item.get("body"), 3000)
            if heading or body:
                sections.append({"heading": heading, "body": body})
    return sections


def _cost_options(args, previous=None):
    options = dict(previous or {})
    for key, low, high in (("down_payment_pct", 0, 100), ("years", 1, 40), ("interest_rate_pct", 0, 25), ("cheques", 1, 12)):
        value = _number(args, key, low, high)
        if value is not None:
            options[key] = value
    return options


def _render_draft(draft, session):
    rows = {r["id"]: r for r in _listings_by_ids(draft["property_ids"])}
    missing = [i for i in draft["property_ids"] if i not in rows]
    if missing:
        raise ToolError(f"No listing with id(s) {missing}.")
    listings = [{**rows[i], "emirate": emirate_of(rows[i]["area"])} for i in draft["property_ids"]]
    costs = {}
    if draft["include_cost_estimate"]:
        for listing in listings:
            costs[listing["id"]] = estimate_costs(listing["price_aed"], listing["listing_type"], listing["emirate"], **draft["cost_options"])
    if draft["kind"] == "property_brochure" and listings:
        cover = listings[0].get("image_url")
    else:  # a skyline photo from the media table
        emirate = listings[0]["emirate"] if listings else "Dubai"
        cover = photo_url(COVER_PHOTO_KEYS.get(emirate, "downtownSkyline")) or photo_url("downtownSkyline")
    build_draft_pdf(
        os.path.join(GENERATED_DIR, f"{draft['draft_id']}.pdf"),
        draft=draft, listings=listings, costs=costs, cover_image_url=cover,
        business_name=BUSINESS_NAME, site_url=website_url(session),
    )
    # The path works with the API address React already has (VITE_AGENT_API_URL + pdf_path);
    # pdf_url is the full link, for Postman and anything outside React.
    draft["pdf_path"] = f"/drafts/{draft['draft_id']}.pdf"
    draft["pdf_url"] = f"{API_BASE_URL}{draft['pdf_path']}"


def draft_public(draft):
    return {
        "draft_id": draft["draft_id"], "kind": draft["kind"], "title": draft["title"], "version": draft["version"],
        "prepared_for": draft["recipient_name"] or None, "property_ids": draft["property_ids"],
        "sections": [s["heading"] or s["body"][:60] for s in draft["sections"]],
        "include_cost_estimate": draft["include_cost_estimate"],
        "pdf_path": draft["pdf_path"], "pdf_url": draft["pdf_url"],
    }


def _check_draft_listings(kind, ids):
    _, fewest, most = DRAFT_KINDS[kind]
    if not fewest <= len(ids) <= most:
        raise ToolError(f"A {kind} needs between {fewest} and {most} property_ids.")


def create_draft(args, session):
    kind = _text(args, "kind", 40)
    if kind not in DRAFT_KINDS:
        raise ToolError(f"'kind' must be one of {list(DRAFT_KINDS)}.")
    title = _text(args, "title", 120)
    if not title:
        raise ToolError("'title' is required.")
    ids = _ids(args.get("property_ids"), 0, 6)
    _check_draft_listings(kind, ids)
    if len(session.drafts) >= 10:
        raise ToolError("This chat already has 10 drafts. Update an existing one instead.")
    draft = {
        "draft_id": uuid.uuid4().hex, "kind": kind, "title": title, "version": 1,
        "recipient_name": _text(args, "recipient_name", 80), "sections": _sections(args.get("sections")),
        "property_ids": ids,
        "include_cost_estimate": kind == "cost_estimate" or _is_yes(args.get("include_cost_estimate")),
        "cost_options": _cost_options(args),
    }
    _render_draft(draft, session)
    session.drafts[draft["draft_id"]] = draft
    session.events["draft"] = draft_public(draft)
    return {"status": "created", "draft": draft_public(draft),
            "message": "Tell the visitor the PDF is ready, sum it up in a sentence, and ask whether to change anything or email it to them."}


def update_draft(args, session):
    draft = session.drafts.get(_text(args, "draft_id", 40))
    if not draft:
        raise ToolError("Unknown 'draft_id'. Use the id create_draft returned in this chat.")
    if args.get("title"):
        draft["title"] = _text(args, "title", 120)
    if "sections" in args:
        draft["sections"] = _sections(args.get("sections"))
    if "property_ids" in args:
        ids = _ids(args.get("property_ids"), 0, 6)
        _check_draft_listings(draft["kind"], ids)
        draft["property_ids"] = ids
    if "recipient_name" in args:
        draft["recipient_name"] = _text(args, "recipient_name", 80)
    if "include_cost_estimate" in args:
        draft["include_cost_estimate"] = draft["kind"] == "cost_estimate" or _is_yes(args.get("include_cost_estimate"))
    draft["cost_options"] = _cost_options(args, draft["cost_options"])
    draft["version"] += 1
    _render_draft(draft, session)
    session.events["draft"] = draft_public(draft)
    return {"status": "updated", "draft": draft_public(draft)}


def email_draft(args, session):
    draft = session.drafts.get(_text(args, "draft_id", 40))
    if not draft:
        raise ToolError("Unknown 'draft_id'. Create the draft first with create_draft.")
    to_email = _text(args, "to_email", 254)
    if not EMAIL_PATTERN.fullmatch(to_email):
        raise ToolError("'to_email' must be the visitor's email address. Ask them for it.")
    to_name = _text(args, "to_name", 80) or draft["recipient_name"]

    if not email_configured():
        return {"status": "email_not_configured", "pdf_url": draft["pdf_url"],
                "message": "Email isn't set up on the server yet. Give the visitor the PDF link instead."}
    if session.emails_sent >= MAX_EMAILS_PER_SESSION:
        return {"status": "limit_reached", "message": "The email limit for this chat is reached. Offer the PDF link instead."}

    summary = {"to": to_email, "name": to_name or None, "document": draft["title"], "version": draft["version"]}
    fingerprint = f"{draft['draft_id']}:{draft['version']}:{to_email.lower()}"
    waiting = _needs_confirmation(session, "email_draft", fingerprint, args.get("confirmed"), summary)
    if waiting:
        return waiting
    if not daily_emails.available():
        return {"status": "limit_reached", "message": "The server's daily email limit is reached. Offer the PDF link instead."}

    pdf_file = draft_pdf_path(draft["draft_id"])
    if pdf_file is None:  # the file is gone (e.g. the disk was cleared): make it again
        _render_draft(draft, session)
        pdf_file = draft_pdf_path(draft["draft_id"])

    site = website_url(session)
    greeting = f"Hello {to_name}," if to_name else "Hello,"
    if site:
        next_step_text = f"Reply to this email, or visit {site}, to arrange a viewing or ask anything else."
        next_step_html = (f"Reply to this email, or visit <a href=\"{html_escape(site)}\">{html_escape(site)}</a>, "
                          "to arrange a viewing or ask anything else.")
    else:
        next_step_text = next_step_html = "Reply to this email to arrange a viewing or ask anything else."
    text_body = (
        f"{greeting}\n\nAs requested in our chat, here is \"{draft['title']}\" (PDF attached).\n\n"
        f"{next_step_text}\n\n{BUSINESS_NAME}"
    )
    html_body = (
        f"<p>{html_escape(greeting)}</p><p>As requested in our chat, here is "
        f"<strong>{html_escape(draft['title'])}</strong> (PDF attached).</p>"
        f"<p>{next_step_html}</p><p>{html_escape(BUSINESS_NAME)}</p>"
    )
    filename = re.sub(r"[^A-Za-z0-9]+", "-", draft["title"]).strip("-")[:60] or "document"
    send_pdf_email(
        to_email=to_email, to_name=to_name, subject=f"{draft['title']} | {BUSINESS_NAME}",
        text_body=text_body, html_body=html_body, pdf_file=pdf_file, filename=f"{filename}.pdf",
    )
    session.emails_sent += 1
    daily_emails.record()
    session.events["email"] = {"status": "sent", "to": to_email, "draft_id": draft["draft_id"], "title": draft["title"]}
    return {"status": "sent", "message": f"The PDF was emailed to {to_email}."}


# ============================================================================
# The catalogue the model sees
# ============================================================================

TOOLS = {
    "search_properties": {
        "fn": search_properties,
        "about": "Find listings. All args optional; combine them.",
        "args": '{"area": "area or emirate name", "emirate": "Dubai | Abu Dhabi", "listing_type": "sale | rent", '
                f'"property_type": "{" | ".join(PROPERTY_TYPES)}", "min_price": number, "max_price": number, '
                '"min_bedrooms": number, "max_bedrooms": number, "min_bathrooms": number, "min_size_sqft": number, '
                '"max_size_sqft": number, "keywords": "words to find in the title, description or amenities, e.g. pool sea view", '
                '"sort": "recommended | price_asc | price_desc | size_desc | newest", "limit": 1-10}',
    },
    "get_property_details": {
        "fn": get_property_details,
        "about": "Full details of one listing: description, amenities, address.",
        "args": '{"property_id": "id"}',
    },
    "find_similar_properties": {
        "fn": find_similar_properties,
        "about": "Listings like a given one (same area, then emirate, closest price).",
        "args": '{"property_id": "id", "limit": 1-6}',
    },
    "compare_properties": {
        "fn": compare_properties,
        "about": "Compare 2-4 listings: price, size, price per sqft, with highlights.",
        "args": '{"property_ids": ["id", "id"]}',
    },
    "list_areas": {
        "fn": list_areas,
        "about": "The areas this website covers, with how many listings each has.",
        "args": '{"emirate": "Dubai | Abu Dhabi (optional)"}',
    },
    "area_market_summary": {
        "fn": area_market_summary,
        "about": "Price range, averages and property types for an area or emirate (from this website's listings).",
        "args": '{"area": "area or emirate name"}',
    },
    "estimate_costs": {
        "fn": estimate_costs_tool,
        "about": "Buying costs (fees, down payment, monthly mortgage) or renting costs (cheques, deposit, fees).",
        "args": '{"property_id": "id"} or {"price_aed": number, "listing_type": "sale | rent", "area": "name"}, '
                'plus optional "down_payment_pct" (20-80), "years" (5-25), "interest_rate_pct" (2.5-8), "cheques" (1, 2, 4 or 12)',
    },
    "save_inquiry": {
        "fn": save_inquiry,
        "about": "Save the visitor's enquiry so an agent contacts them. Asks for confirmation first (see rules).",
        "args": '{"full_name": "", "email": "", "phone": "", "area": "", "listing_type": "sale | rent", '
                f'"property_type": "{" | ".join(PROPERTY_TYPES)}", "budget_aed": number, "bedrooms": number (optional), '
                '"message": "optional note", "property_id": "id (optional, fills area/type/budget)", "confirmed": false}',
    },
    "create_draft": {
        "fn": create_draft,
        "about": "Make a PDF document. Kinds: " + "; ".join(f"{k} = {v[0]}" for k, v in DRAFT_KINDS.items()),
        "args": '{"kind": "kind", "title": "document title", "recipient_name": "visitor name (optional)", '
                '"property_ids": ["id"], "sections": [{"heading": "", "body": "plain text; blank line between paragraphs"}], '
                '"include_cost_estimate": false, plus optional cost options like estimate_costs}',
    },
    "update_draft": {
        "fn": update_draft,
        "about": "Change a draft (title, sections, property_ids, recipient_name, include_cost_estimate). Makes a new version.",
        "args": '{"draft_id": "id", ...only the fields to change}',
    },
    "email_draft": {
        "fn": email_draft,
        "about": "Email a draft's PDF to the visitor from the agency's Gmail. Asks for confirmation first (see rules).",
        "args": '{"draft_id": "id", "to_email": "visitor email", "to_name": "optional", "confirmed": false}',
    },
}


def tools_for_prompt():
    return "\n".join(f"- {name}: {tool['about']}\n  args: {tool['args']}" for name, tool in TOOLS.items())


def run_tool(name, args, session):
    """Run one tool and always return a dict. Problems come back as {"error": ...}
    so the model can correct itself or explain to the visitor."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": "unknown_tool", "message": f"There is no tool called {name!r}."}
    if not isinstance(args, dict):
        return {"error": "invalid_arguments", "message": "'args' must be a JSON object."}
    try:
        return tool["fn"](args, session)
    except ToolError as err:
        return {"error": "invalid_arguments", "message": str(err)}
    except ForbiddenTableError as err:
        log.error("Blocked table access in %s: %s", name, err)
        return {"error": "forbidden", "message": str(err)}
    except DatabaseError as err:
        log.warning("Database problem in %s: %s", name, err)
        return {"error": "database_unavailable", "message": "The listings database didn't respond. Try again shortly."}
    except EmailError as err:
        log.warning("Email problem in %s: %s", name, err)
        return {"error": "email_failed", "message": str(err)}
    except Exception:
        log.exception("Tool %s crashed", name)
        return {"error": "internal_error", "message": "The tool failed unexpectedly."}


# ============================================================================
# Start-up print: the endpoints to give the React website
# ============================================================================

def print_endpoints():
    """Printed when the server starts (main.py imports this file). On Render it shows in the Logs tab."""
    line = "=" * 72
    print("\n".join([
        line,
        f" {BUSINESS_NAME} - agent API",
        f" Base URL        : {API_BASE_URL}",
        f" Chat            : POST   {API_BASE_URL}/chat",
        f" Conversation    : GET    {API_BASE_URL}/sessions/<session_id>",
        f" End conversation: DELETE {API_BASE_URL}/sessions/<session_id>",
        f" PDF drafts      : GET    {API_BASE_URL}/drafts/<draft_id>.pdf",
        f" API docs        : {API_BASE_URL}/docs",
        "",
        " Put this in the React website's .env, then start the website:",
        f"   VITE_AGENT_API_URL={API_BASE_URL}",
        "",
        f" Website URL     : {WEBSITE_URL or 'not set - taken from the browser (window.location.origin)'}",
        f" Email           : {email_method() or 'not set up - add GOOGLE_REFRESH_TOKEN (or token.json); the agent offers the PDF link meanwhile'}",
        line,
    ]), flush=True)


print_endpoints()
